"""Self-test for the mcgs-max MCP server.

Runs every tool against a scratch window, covering the normal path, boundary
values, wrong-type arguments and mixed pages, then restores the scratch window.
Exit code 0 means everything passed.

    python selftest.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import os

PY = sys.executable
RUN = Path(__file__).with_name("run.py")

# 跑之前指定：MCGS_SELFTEST_PROJECT=工程名  MCGS_SELFTEST_WINDOW=空白窗口名
PROJECT = os.environ.get("MCGS_SELFTEST_PROJECT", "")
SCRATCH = os.environ.get("MCGS_SELFTEST_WINDOW", "")

results = []


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [PY, RUN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self._id = 0
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "1"}}})
        self.read()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed")
        return json.loads(line)

    # Tools that act on a project. The selftest always names one, so it cannot
    # touch a different project just because MCGS enumerated it first.
    PROJECT_TOOLS = ("mcgs_get_window", "mcgs_label_add", "mcgs_control_add",
                     "mcgs_control_update", "mcgs_controls_update",
                     "mcgs_control_delete")

    def call(self, name, args=None, raw=False):
        args = dict(args or {})
        if not raw and name in self.PROJECT_TOOLS and "project" not in args and PROJECT:
            args["project"] = PROJECT
        self._id += 1
        self.send({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                   "params": {"name": name, "arguments": args}})
        msg = self.read()
        if "error" in msg:
            return {"__rpc_error__": msg["error"]}
        return json.loads(msg["result"]["content"][0]["text"])

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<46} {detail}")


def check_library_action():
    """The CAniClick expression sits 53 bytes after its class tag."""
    lib = Path(r"D:\McgsPro\Program\library\public\公共图库.lib")
    if not lib.exists():
        check("图库存在", False, str(lib))
        return False

    sys.path.insert(0, r"D:\mcgs-max")
    import shutil
    import tempfile
    from mcgs_max import compat
    from mcgs_max.controls import click_variable

    tmp = Path(tempfile.gettempdir()) / "selftest_lib.lib"
    shutil.copy2(lib, tmp)
    try:
        db = compat.require("access_parser").AccessParser(str(tmp))
        comp = db.parse_table("Component")
    finally:
        tmp.unlink(missing_ok=True)

    hits = 0
    sample = ""
    for name, blob in zip(comp["strName"], comp["lbContext"]):
        if not blob:
            continue
        value = click_variable(bytes(blob))
        if value:
            hits += 1
            if not sample:
                sample = f"{name} -> {value}"

    check("图库按钮动作变量可读", hits > 100, f"{hits} 个，例: {sample}")
    return hits > 100


def clear(client):
    page = client.call("mcgs_get_window", {"window": SCRATCH})
    for _ in range(page.get("control_count", 0)):
        client.call("mcgs_control_delete", {"window": SCRATCH, "index": 0})


PROJECT_WINDOWS: list[str] = []


def resolve_target(client) -> str:
    """Pick the project the suite works on. Refuses to guess silently."""
    info = client.call("mcgs_get_project")
    opened = []
    for inst in info.get("instances", []):
        for pr in inst.get("projects", []):
            opened.append(pr)
    if not opened:
        return ""
    if len(opened) > 1:
        names = "、".join(p["name"] for p in opened)
        print(f"打开了 {len(opened)} 个工程（{names}），用第一个「{opened[0]['name']}」做沙盘。")
    global PROJECT_WINDOWS
    PROJECT_WINDOWS = [w["name"] for w in opened[0].get("windows", [])]
    return opened[0]["name"]


def main():
    global PROJECT
    if not SCRATCH:
        raise SystemExit(
            "没有指定沙盘窗口。运行前设置环境变量：\n"
            "    $env:MCGS_SELFTEST_WINDOW = \"空白窗口名\"\n"
            "    $env:MCGS_SELFTEST_PROJECT = \"工程名\"    # 可选，只有一个工程时自动取\n"
            "沙盘窗口会被反复清空重建，必须是一个可以随便改的空窗口。")

    c = Client()
    PROJECT = resolve_target(c)

    # The suite rewrites its scratch window from scratch. Refuse to run against a
    # window that already holds something, so a stray invocation cannot wipe a
    # page the user is working on.
    existing = c.call("mcgs_get_window", {"window": SCRATCH})
    if not existing.get("ok"):
        print(f"沙盘窗口 {SCRATCH!r} 打不开: {existing.get('error_code')}")
        print("请先在 MCGS 里建一个空的用户窗口，或修改 SCRATCH 常量。")
        c.close()
        return 2
    if existing.get("control_count"):
        print(f"沙盘窗口 {SCRATCH!r} 里已有 {existing['control_count']} 个控件，拒绝清空。")
        print("请换一个空窗口（修改 SCRATCH 常量），或先手动清空它。")
        c.close()
        return 2

    print("=" * 78)
    print("协议")
    print("=" * 78)
    c.send({"jsonrpc": "2.0", "id": 900, "method": "bogus/method"})
    msg = c.read()
    check("未知方法返回 -32601", msg.get("error", {}).get("code") == -32601)

    print()
    print("=" * 78)
    print("读工具")
    print("=" * 78)
    t = time.time()
    r = c.call("mcgs_get_project")
    check("get_project", r.get("ok"), f"{int((time.time()-t)*1000)}ms")
    check("  projects 非空", bool(r.get("instances", [{}])[0].get("projects")))

    probe = next((w for w in PROJECT_WINDOWS if w != SCRATCH), None)
    if probe:
        r = c.call("mcgs_get_window", {"window": probe})
        check(f"get_window({probe})", r.get("ok"), f"controls={r.get('control_count')}")
    r = c.call("mcgs_get_window", {"window": "不存在的窗口"})
    check("get_window 不存在 -> WINDOW_NOT_FOUND",
          r.get("error_code") == "WINDOW_NOT_FOUND")

    print()
    print("=" * 78)
    print("写工具：四类控件")
    print("=" * 78)
    clear(c)
    check("清空沙盘", c.call("mcgs_get_window", {"window": SCRATCH}).get("control_count") == 0)

    r = c.call("mcgs_label_add", {
        "window": SCRATCH, "caption": "标签A", "x": 40, "y": 40,
        "width": 180, "height": 60, "font_size": 20,
        "fill": "#FFFF00", "text_color": "#FF0000", "border_color": "#0000FF"})
    ok = r.get("ok")
    ctl = r["controls"][0] if ok else {}
    check("label_add", ok,
          f"caption={ctl.get('caption')!r} fill={ctl.get('fill')} fg={ctl.get('text_color')}")
    check("  caption/填充/字色正确",
          ctl.get("caption") == "标签A" and ctl.get("fill") == "#FFFF00"
          and ctl.get("text_color") == "#FF0000")

    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "button", "x": 40, "y": 120,
        "width": 180, "height": 60, "text_off": "启动", "text_font": "微软雅黑"})
    ok = r.get("ok")
    btn = next((x for x in r.get("controls", []) if x["kind"] == "button"), {})
    check("control_add(button)", ok, f"texts={btn.get('texts')}")
    check("  按钮文字正确", btn.get("texts") == ["启动"])

    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "bit_switch", "x": 40, "y": 200,
        "width": 180, "height": 70, "text_off": "关状态", "text_on": "开状态"})
    ok = r.get("ok")
    sw = next((x for x in r.get("controls", []) if x["kind"] == "bit_switch"), {})
    check("control_add(bit_switch)", ok, f"texts={sw.get('texts')}")
    check("  开关双态文字正确", sw.get("texts") == ["关状态", "开状态"])

    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "lamp", "x": 40, "y": 290,
        "width": 120, "height": 120})
    check("control_add(lamp)", r.get("ok"), f"controls={r.get('control_count')}")

    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "numeric_input", "x": 200, "y": 200,
        "width": 160, "height": 70})
    check("control_add(numeric_input)", r.get("ok"), f"controls={r.get('control_count')}")

    page = c.call("mcgs_get_window", {"window": SCRATCH})
    check("混合页面共 5 个控件", page.get("control_count") == 5,
          f"kinds={page.get('kinds')}")

    print()
    print("=" * 78)
    print("改工具")
    print("=" * 78)
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "caption": "标签B", "x": 60, "font_size": 26})
    ok = r.get("ok")
    ctl = r["controls"][0] if ok else {}
    check("control_update(label)", ok,
          f"caption={ctl.get('caption')!r} rect={ctl.get('rect')}")
    check("  文字/位置/字号生效",
          ctl.get("caption") == "标签B" and ctl.get("rect", [0])[0] == 60
          and ctl.get("font", {}).get("height") == -26)

    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 1, "text_off": "运行中", "fill": "#CCFFCC"})
    ok = r.get("ok")
    btn = r["controls"][1] if ok else {}
    check("control_update(button 文字+填充)", ok, f"texts={btn.get('texts')}")

    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 2, "text_on": "已启动", "text_font": "黑体"})
    ok = r.get("ok")
    sw = r["controls"][2] if ok else {}
    check("control_update(switch 开态文字+字体)", ok,
          f"texts={sw.get('texts')} faces={sw.get('text_faces')}")

    print()
    print("=" * 78)
    print("参数用错类型")
    print("=" * 78)
    r = c.call("mcgs_control_update", {"window": SCRATCH, "index": 4, "text_off": "x"})
    check("输入框改文字 -> UNSUPPORTED_CONTROL",
          r.get("error_code") == "UNSUPPORTED_CONTROL")
    r = c.call("mcgs_control_update", {"window": SCRATCH, "index": 4, "caption": "x"})
    check("输入框改 caption -> UNSUPPORTED_CONTROL",
          r.get("error_code") == "UNSUPPORTED_CONTROL")
    r = c.call("mcgs_control_update", {"window": SCRATCH, "index": 0, "text_off": "x"})
    check("标签改 text_off -> UNSUPPORTED_CONTROL",
          r.get("error_code") == "UNSUPPORTED_CONTROL")
    r = c.call("mcgs_control_update", {"window": SCRATCH, "index": 99, "x": 1})
    check("序号越界 -> INDEX_OUT_OF_RANGE",
          r.get("error_code") == "INDEX_OUT_OF_RANGE")
    r = c.call("mcgs_control_add", {"window": SCRATCH, "kind": "nope",
                                    "x": 0, "y": 0, "width": 10, "height": 10})
    check("未知 kind -> BAD_ARGUMENT", r.get("error_code") == "BAD_ARGUMENT")
    r = c.call("mcgs_label_add", {"window": SCRATCH, "caption": "x", "x": 10,
                                  "y": 10, "width": 0, "height": 10})
    check("宽为 0 -> BAD_RECT", r.get("error_code") == "BAD_RECT")
    r = c.call("mcgs_label_add", {"window": SCRATCH, "caption": "x", "x": 10,
                                  "y": 10, "width": 100, "height": 50,
                                  "fill": "notacolor"})
    check("非法颜色 -> BAD_ARGUMENT", r.get("error_code") == "BAD_ARGUMENT")
    r = c.call("mcgs_label_add", {"window": "不存在", "caption": "x", "x": 0,
                                  "y": 0, "width": 10, "height": 10})
    check("窗口不存在 -> WINDOW_NOT_FOUND",
          r.get("error_code") == "WINDOW_NOT_FOUND")

    print()
    print("=" * 78)
    print("删工具")
    print("=" * 78)
    r = c.call("mcgs_control_delete", {"window": SCRATCH, "index": 4})
    check("control_delete", r.get("ok"), f"controls={r.get('control_count')}")
    check("  剩余 4 个", r.get("control_count") == 4)
    r = c.call("mcgs_control_delete", {"window": SCRATCH, "index": 99})
    check("删除越界 -> INDEX_OUT_OF_RANGE",
          r.get("error_code") == "INDEX_OUT_OF_RANGE")

    print()
    print("=" * 78)
    print("按钮动作")
    print("=" * 78)
    clear(c)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "button", "x": 40, "y": 40,
        "width": 200, "height": 70, "text_off": "按钮"})
    check("建一个无动作按钮", r.get("ok"))
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "action_variable": "@测试"})
    check("无动作按钮改动作 -> 拒绝",
          r.get("error_code") == "UNSUPPORTED_CONTROL",
          str(r.get("message", ""))[:50])
    clear(c)

    print()
    print("=" * 78)
    print("工程选择")
    print("=" * 78)
    proj = c.call("mcgs_get_project")
    opened = []
    for inst in proj.get("instances", []):
        for pr in inst.get("projects", []):
            opened.append(pr)
    check("至少有一个打开的工程", bool(opened), str(len(opened)))

    if opened:
        name = opened[0]["name"]
        r = c.call("mcgs_get_window", {"window": SCRATCH, "project": name})
        check(f"按工程名指定 ({name})", r.get("ok"), str(r.get("error_code")))

        r = c.call("mcgs_get_window", {"window": SCRATCH, "project": opened[0]["path"]})
        check("按完整路径指定", r.get("ok"), str(r.get("error_code")))

        # 片段要唯一才算成功；多工程时用去掉首字的写法
        fragment = name[1:] if len(name) > 1 else name
        r = c.call("mcgs_get_window", {"window": SCRATCH, "project": fragment})
        ok = r.get("ok") or r.get("error_code") == "PROJECT_AMBIGUOUS"
        check(f"按片段指定 ({fragment})", ok, str(r.get("error_code")))

        # 不给 project 必须被拒绝——工具不猜。
        # schema 里 project 是必填，所以会在参数校验层就被挡下；
        # 传空串则走到 resolve_project，返回 PROJECT_REQUIRED。
        r = c.call("mcgs_get_window", {"window": SCRATCH}, raw=True)
        rejected = "__rpc_error__" in r or r.get("error_code") == "PROJECT_REQUIRED"
        check("不传 project -> 拒绝", rejected, str(r)[:50])

        r = c.call("mcgs_get_window", {"window": SCRATCH, "project": "  "}, raw=True)
        check("project 传空串 -> PROJECT_REQUIRED",
              r.get("error_code") == "PROJECT_REQUIRED", str(r.get("error_code")))

        # 缺 window 由 schema 拦截
        r = c.call("mcgs_get_window", {"project": name}, raw=True)
        check("不传 window -> 参数错误",
              not r.get("ok"), str(r.get("error_code") or r.get("message", ""))[:40])

    r = c.call("mcgs_get_window", {"window": SCRATCH, "project": "根本不存在的工程"})
    check("不存在的工程 -> PROJECT_NOT_FOUND",
          r.get("error_code") == "PROJECT_NOT_FOUND", str(r.get("message", ""))[:50])

    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "label", "x": 10, "y": 10,
        "width": 50, "height": 20, "project": "根本不存在的工程"})
    check("写操作带错工程 -> 拒绝",
          r.get("error_code") == "PROJECT_NOT_FOUND", str(r.get("error_code")))
    clear(c)

    print()
    print("=" * 78)
    print("输入框数值格式")
    print("=" * 78)
    clear(c)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "numeric_input", "x": 40, "y": 40,
        "width": 200, "height": 70})
    check("建一个输入框", r.get("ok"))

    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0,
        "decimals": 3, "min_integer_digits": 2,
        "unit": "V", "unit_enabled": True,
        "minimum": -50.0, "maximum": 50.0})
    nf = next((x.get("number_format") for x in r.get("controls", [])
               if x["kind"] == "numeric_input"), {})
    check("小数位/整数位/单位/范围一起设",
          r.get("ok") and nf.get("decimals") == 3
          and nf.get("min_integer_digits") == 2
          and nf.get("unit") == "V"
          and nf.get("unit_enabled") is True
          and nf.get("maximum") == 50.0 and nf.get("minimum") == -50.0,
          str(nf))

    # 自然小数的读回不可靠（MCGS 有时把小数位写成 0、有时写成 -1），
    # 这里只验证写入被接受，具体数值用 MCGS 属性对话框确认过。
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "natural_decimals": True})
    check("开自然小数被接受", r.get("ok"), str(r.get("check_errors")))

    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "decimals": 2})
    nf = next((x.get("number_format") for x in r.get("controls", [])
               if x["kind"] == "numeric_input"), {})
    check("再设小数位 2 生效", r.get("ok") and nf.get("decimals") == 2, str(nf))

    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "unit": "Hz-long"})
    nf = next((x.get("number_format") for x in r.get("controls", [])
               if x["kind"] == "numeric_input"), {})
    check("单位文字可变长", r.get("ok") and nf.get("unit") == "Hz-long", str(nf))

    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "unit_enabled": False})
    nf = next((x.get("number_format") for x in r.get("controls", [])
               if x["kind"] == "numeric_input"), {})
    check("单位可关闭", r.get("ok") and nf.get("unit_enabled") is False, str(nf))

    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "decimals": 3, "natural_decimals": True})
    check("小数位与自然小数同时给 -> 拒绝",
          r.get("error_code") == "BAD_ARGUMENT", str(r.get("message", ""))[:50])

    clear(c)
    r = c.call("mcgs_label_add", {
        "window": SCRATCH, "caption": "标签", "x": 40, "y": 40,
        "width": 160, "height": 60})
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "decimals": 2})
    check("标签设数值格式 -> UNSUPPORTED_CONTROL",
          r.get("error_code") == "UNSUPPORTED_CONTROL")
    clear(c)

    print()
    print("=" * 78)
    print("按钮文字长度")
    print("=" * 78)
    clear(c)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "button", "x": 40, "y": 40,
        "width": 240, "height": 70})
    check("建一个按钮", r.get("ok"))
    for text in ("短", "正好十个字的按钮名", "这是一段很长的按钮文字用来验证增长是否正常"):
        r = c.call("mcgs_control_update", {
            "window": SCRATCH, "index": 0, "text_off": text})
        got = (r.get("controls") or [{}])[0].get("texts")
        check(f"设 {len(text)} 字", r.get("ok") and got == [text], f"读回={got}")
    clear(c)

    print()
    print("=" * 78)
    print("按钮动作 5 种模式")
    print("=" * 78)
    clear(c)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "bit_switch", "x": 40, "y": 40,
        "width": 200, "height": 100})
    check("建一个开关", r.get("ok"))
    for mode in ("置1", "按1松0", "置0", "按0松1", "取反"):
        r = c.call("mcgs_control_update", {
            "window": SCRATCH, "index": 0, "action_mode": mode})
        got = next((x.get("action_mode") for x in r.get("controls", [])
                    if "action_mode" in x), None)
        check(f"设 {mode} 读回一致", r.get("ok") and got == mode, f"读回={got!r}")
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "action_mode": "不存在"})
    check("非法模式 -> BAD_ARGUMENT", r.get("error_code") == "BAD_ARGUMENT")
    clear(c)

    print()
    print("=" * 78)
    print("外观生效范围")
    print("=" * 78)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "lamp", "x": 40, "y": 40,
        "width": 120, "height": 120, "fill": "#00FF00"})
    check("图片外观控件设填充 -> 有警告",
          r.get("ok") and bool(r.get("warnings")), str(r.get("warnings")))

    clear(c)
    r = c.call("mcgs_label_add", {
        "window": SCRATCH, "caption": "标签", "x": 40, "y": 40,
        "width": 160, "height": 60, "fill": "#00FF00"})
    check("文字标签设填充 -> 无警告",
          r.get("ok") and not r.get("warnings"), str(r.get("warnings")))
    clear(c)

    print()
    print("=" * 78)
    print("按钮动作变量（读 MCGS 公共图库，不碰工程）")
    print("=" * 78)
    lib_ok = check_library_action()

    print()
    print("=" * 78)
    print("设备与变量")
    print("=" * 78)

    dev = c.call("mcgs_get_device", {"project": PROJECT})
    devices = [d for w in dev.get("device_windows", []) for d in w.get("devices", [])]
    check("mcgs_get_device 返回设备", bool(devices), f"{len(devices)} 个")
    channels = []
    if devices:
        channels = [ch["channel"] for ch in devices[0].get("channels", [])]
        check("设备带通道表", bool(channels), f"{len(channels)} 个通道")
        check("设备类型非空", bool(devices[0].get("type")), devices[0].get("type", ""))

    if not channels:
        print("  没有通道，跳过变量测试")
        clear(c)
        c.close()
        print()
        print("=" * 78)
        passed = sum(1 for _, ok, _ in results if ok)
        print(f"结果: {passed}/{len(results)} 通过（已跳过变量测试）")
        return 0 if passed == len(results) else 1

    clear(c)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "numeric_input", "x": 40, "y": 40,
        "width": 200, "height": 70})
    check("建一个输入框用于绑定", r.get("ok"))

    # 未绑定状态
    page = c.call("mcgs_get_window", {"window": SCRATCH})
    first = page["controls"][0] if page.get("controls") else {}
    check("新建控件没有变量", not first.get("variable"), str(first.get("variable")))

    # 从无到有绑定
    target = channels[0]
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "variable": target})
    got = (r.get("controls") or [{}])[0].get("variable")
    check(f"从无到有绑定 ({target})", r.get("ok") and got == target, str(got))

    # 改绑
    target2 = channels[1] if len(channels) > 1 else channels[0]
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "variable": target2})
    got = (r.get("controls") or [{}])[0].get("variable")
    check(f"改绑 ({target2})", r.get("ok") and got == target2, str(got))

    # 解绑
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "variable": ""})
    got = (r.get("controls") or [{}])[0].get("variable")
    check("解绑", r.get("ok") and not got, str(got))

    # 未知通道
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "variable": "设备X_不存在"})
    check("未知通道 -> UNKNOWN_VARIABLE",
          r.get("error_code") == "UNKNOWN_VARIABLE", str(r.get("error_code")))

    # 指示灯和开关也能绑
    for kind, ch in (("lamp", channels[2] if len(channels) > 2 else channels[0]),
                     ("bit_switch", channels[3] if len(channels) > 3 else channels[0])):
        clear(c)
        r = c.call("mcgs_control_add", {
            "window": SCRATCH, "kind": kind, "x": 40, "y": 40,
            "width": 150, "height": 100})
        if not r.get("ok"):
            check(f"{kind} 新建", False, str(r.get("error_code")))
            continue
        r = c.call("mcgs_control_update", {
            "window": SCRATCH, "index": 0, "variable": ch})
        got = (r.get("controls") or [{}])[0].get("variable")
        check(f"{kind} 绑定 ({ch})", r.get("ok") and got == ch, str(got))

    # 按钮：通道名必须被拒
    clear(c)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "button", "x": 40, "y": 40,
        "width": 200, "height": 70})
    if r.get("ok"):
        r = c.call("mcgs_control_update", {
            "window": SCRATCH, "index": 0, "variable": channels[0]})
        check("按钮传通道名 -> 拒绝",
              r.get("error_code") == "UNSUPPORTED_CONTROL", str(r.get("error_code")))
    clear(c)

    # 多组字段必须同时生效：早先每组各自 return，后面的会被静默丢弃
    clear(c)
    c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "numeric_input", "x": 40, "y": 40,
        "width": 200, "height": 70})
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0,
        "variable": channels[0], "x": 300, "y": 400})
    ctl = (r.get("controls") or [{}])[0]
    got_var = ctl.get("variable")
    got_rect = ctl.get("rect")
    check("多组字段同时生效（变量+位置）",
          r.get("ok") and got_var == channels[0]
          and got_rect and got_rect[0] == 300 and got_rect[1] == 400,
          f"变量={got_var} rect={got_rect}")

    # 首次绑定会重置数值格式，所以格式要在绑定之后再设
    r = c.call("mcgs_control_update", {
        "window": SCRATCH, "index": 0, "decimals": 3, "minimum": 0, "maximum": 99})
    ctl = (r.get("controls") or [{}])[0]
    fmt = ctl.get("number_format") or {}
    check("绑定后再设数值格式生效",
          r.get("ok") and fmt.get("decimals") == 3 and fmt.get("maximum") == 99,
          str(fmt))

    # 按钮的动作是脚本：既不能当通道名改，也不能清空
    clear(c)
    r = c.call("mcgs_control_add", {
        "window": SCRATCH, "kind": "button", "x": 40, "y": 40,
        "width": 200, "height": 70})
    if r.get("ok"):
        r = c.call("mcgs_control_update", {
            "window": SCRATCH, "index": 0, "variable": ""})
        check("按钮解绑 -> 拒绝",
              r.get("error_code") == "UNSUPPORTED_CONTROL", str(r.get("error_code")))

        # 按钮的动作是脚本，传完整脚本应被接受（脚本内容不按通道名校验）
        script = channels[0] + "=" + channels[0] + "*1.01"
        r = c.call("mcgs_control_update", {
            "window": SCRATCH, "index": 0, "variable": script})
        got = (r.get("controls") or [{}])[0].get("variable")
        check("按钮传完整脚本 -> 接受",
              r.get("ok") and got == script, str(got)[:40])
    clear(c)

    print()
    print("=" * 78)
    print("批量更新")
    print("=" * 78)
    for kind, y in (("numeric_input", 40), ("lamp", 130), ("bit_switch", 250)):
        c.call("mcgs_control_add", {
            "window": SCRATCH, "kind": kind, "x": 40, "y": y,
            "width": 160, "height": 80})
    page = c.call("mcgs_get_window", {"window": SCRATCH})
    check("建了 3 个控件", page.get("control_count") == 3, str(page.get("control_count")))

    r = c.call("mcgs_controls_update", {
        "window": SCRATCH,
        "updates": [
            {"index": 0, "variable": channels[0]},
            {"index": 1, "variable": channels[1] if len(channels) > 1 else channels[0]},
            {"index": 2, "variable": channels[2] if len(channels) > 2 else channels[0]},
        ]})
    check("批量绑定 ok", r.get("ok"), str(r.get("check_errors")))
    check("返回 updated 三个", r.get("updated") == [0, 1, 2], str(r.get("updated")))
    bound = [x.get("variable") for x in r.get("controls", [])]
    check("批量结果都绑上了", all(bound) and len(bound) == 3, str(bound))

    r = c.call("mcgs_controls_update", {
        "window": SCRATCH, "updates": [{"index": 0, "variable": ""}]})
    check("批量解绑", r.get("ok") and not (r.get("controls") or [{}])[0].get("variable"),
          str((r.get("controls") or [{}])[0].get("variable")))

    r = c.call("mcgs_controls_update", {
        "window": SCRATCH, "updates": [{"index": 99, "variable": channels[0]}]})
    check("批量越界 -> INDEX_OUT_OF_RANGE",
          r.get("error_code") == "INDEX_OUT_OF_RANGE", str(r.get("error_code")))

    r = c.call("mcgs_controls_update", {
        "window": SCRATCH, "updates": [{"index": 0, "no_such_field": 1}]})
    check("批量未知字段 -> BAD_ARGUMENT",
          r.get("error_code") == "BAD_ARGUMENT", str(r.get("error_code")))

    clear(c)

    print()
    print("=" * 78)
    print("清理沙盘")
    print("=" * 78)
    clear(c)
    page = c.call("mcgs_get_window", {"window": SCRATCH})
    check("沙盘已清空", page.get("control_count") == 0)

    c.close()

    print()
    print("=" * 78)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    failed = [(n, d) for n, ok, d in results if not ok]
    for name, detail in failed:
        print(f"  失败: {name}  {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
