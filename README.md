# mcgs-max

> **干活前先读 [RAG.md](RAG.md)** —— 那是配套知识库，讲清楚每类控件怎么改、
> 变量存在哪、标准工作流怎么走。本 README 是接口说明和实现细节。

McgsPro（MCGS 组态软件）的 MCP 服务器。

**stdio 传输**：客户端自己启动进程、通过 stdin/stdout 通信。**不需要启动服务，不占端口。**

所有信息在运行时从进程和工程文件里发现，不依赖任何写死的路径。

## 目录

```
mcgs-max/
├── run.py             入口（从任意工作目录都能用）
├── selftest.py        自检
├── RAG.md             配套知识库（给 agent 看的操作手册）
├── README.md
├── mcgs_max/
│   ├── __init__.py
│   ├── compat.py      依赖加载（vendor/ 自动入 sys.path）
│   ├── win32.py       Win32 窗口/进程探测、工程解析
│   ├── project.py     .MCP 工程文件读取
│   ├── controls.py    控件解码（坐标/文字/颜色/字体/变量）
│   ├── device.py      设备窗口解析（PLC 与通道表）
│   ├── binding.py     变量绑定读写
│   ├── builder.py     控件构造（模板、文字、颜色、格式、动作模式）
│   ├── templates.json 控件模板（6 种）
│   ├── window.py      用户窗口读取
│   ├── page.py        整页重建、失败回滚
│   ├── workbench.py   工作台列表、打开画布、组态检查
│   ├── labels.py      增删改的业务逻辑
│   ├── clipboard.py   剪贴板私有格式发布
│   ├── tools.py       工具注册与参数校验
│   └── server.py      stdio JSON-RPC 循环
└── vendor/            第三方依赖（自带，不装全局）
```

## 依赖

只有一个是第三方的：`access-parser`（读 .MCP 的 Access/Jet 数据库）。

它装在 `vendor/` 里，`compat.py` 会自动加入 `sys.path`，**不需要 pip install 到系统环境**。

换机器时把整个 `mcgs-max` 目录拷过去即可。若 `vendor/` 丢失：

```powershell
python -m pip install --target D:\mcgs-max\vendor access-parser
```

Python 3.10+ 即可，其余全是标准库。

## 安装与移植

整个目录是自包含的：代码 + `vendor/` 里的第三方库，合计约 1.6 MB。
**拷过去就能跑，不需要 pip install、不需要注册 COM 组件、不需要管理员权限。**

### 前提条件

| 条件 | 说明 |
|---|---|
| Windows | 全部走 Win32 API |
| 已安装 McgsPro | 工具驱动的是**正在运行的**组态环境，不是离线改文件 |
| MCGS 中已打开工程 | 否则返回 `NO_PROJECT_OPEN_OPEN` |
| Python 3.9 以上，64 位 | 不需要 32 位 |

### 移植步骤

**① 拷贝目录**

把整个 `mcgs-max` 目录复制到目标机器，位置随意 —— `run.py` 会自己把项目根目录加进
`sys.path`，不依赖工作目录。

**② 确认目标机器的 Python**

```powershell
where.exe python
# 没有输出就用 DSH 自带的：
Test-Path "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
```

`python` 不在 PATH 上时，配置里必须写解释器的**绝对路径**。

**③ 先在命令行验一遍**

```powershell
<python路径> D:\mcgs-max\run.py
```

粘贴下面这行回车：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}
```

返回 `"serverInfo":{"name":"mcgs-max","version":"0.1.0"}` 即为正常。
stderr 会打印版本号和工具列表，这是日志，不是错误。

**④ 写客户端配置**

DSH 通过 `@deepseek-ai/dsh-mcp-manager` 挂 MCP 服务器。配置写在 profile 的
`cordis.patch.yml` 里，位于 **`mcp-manager` 的 `config.servers` 下**：

```yaml
- id: mcp-manager
  name: "@deepseek-ai/dsh-mcp-manager"
  config:
    servers:
      # … 已有的服务器保持不变 …
      mcgs-max:
        transport: stdio
        command: "C:/Users/<用户>/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"
        args:
          - "D:/mcgs-max/run.py"
        toolCallTimeoutMs: 300000
        startupTimeoutMs: 50000
```

**换机器只需要改 `command` 和 `args` 两行。** 路径用正斜杠，避免 YAML 反斜杠转义。

其他客户端（Codex 等）指向 `run.py` 即可：

```toml
# ~/.codex/config.toml
[mcp_servers.mcgs-max]
command = "<python 绝对路径>"
args = ["D:/mcgs-max/run.py"]
tool_timeout_sec = 120
```

**⑤ 重启或触发重载**

保存 `cordis.patch.yml` 后 DSH 会热重载 profile，十几秒后工具列表出现 8 个 `mcgs_*` 工具。

**⑥ 验证**

```
mcgs_get_project    列出工程即为连通
mcgs_get_device     能读到 PLC 通道表
```

### 配套知识库

`RAG.md` 是给 agent 看的操作手册（工具用法、变量位置、配方、错误码），
和 MCP 一起挂到知识库服务上。agent 检索到它就知道该怎么干活。

## 工具

共 8 个。读的三个约 100 ms；写的五个各约 10 秒（整页重建 + 保存 + 组态检查），
窗口越大越慢，同一页的多处修改用 mcgs_controls_update 合并成一次。

| 工具 | 作用 | 必填 |
|---|---|---|
| `mcgs_get_project` | 工程路径、窗口清单、画布状态、阻塞对话框 | 无 |
| `mcgs_get_device` | PLC 设备、驱动、IP、完整通道表 | `project` |
| `mcgs_get_window` | 读画面：全部控件的坐标/文字/颜色/字体/变量/数值格式 | `window` `project` |
| `mcgs_label_add` | 加一个文字标签 | `window` `caption` `x` `y` `width` `height` `project` |
| `mcgs_control_add` | 加按钮 / 开关 / 指示灯 / 输入框 | `window` `kind` `x` `y` `width` `height` `project` |
| `mcgs_control_update` | 改一个控件（任意类型） | `window` `index` `project` |
| `mcgs_controls_update` | 批量改，整页只重建一次 | `window` `updates` `project` |
| `mcgs_control_delete` | 删一个控件（任意类型） | `window` `index` `project` |

**`window` 和 `project` 都是必填**，工具不做猜测。`project` 接受完整路径、工程名或唯一片段。

`mcgs_control_update` 的可选参数共 25 个：

```
文字   caption  text_off  text_on  text_font  font_size  font_face  font_weight
颜色   fill  text_color  border_color  border_width
位置   x  y  width  height
按钮   action_variable  action_mode
变量   variable
输入框 decimals  natural_decimals  min_integer_digits
       unit  unit_enabled  minimum  maximum
```

`fill` 传 `"none"` 表示无填充；颜色一律 `#RRGGBB`。

`mcgs_controls_update` 的 `updates` 是数组，每项必填 `index`，其余字段与上表相同。
同一页的多处修改用它合并成一次重建。

窗口本身的新建、改名、删除没有做成工具：MCGS 不允许给新窗口命名（列表不支持就地改名、
菜单没有重命名、属性对话框用程序消息打不开），唯一可行的数据库改名必须重开工程才生效。
这些操作在 MCGS 里手动做更快。

### `mcgs_get_project`

获取当前 MCGS 中打开的工程信息。只读。
```jsonc
{
  "ok": true,
  "process_count": 1,
  "instances": [{
    "pid": 8092,
    "frame_title": "McgsPro组态环境 - 动画组态窗口0",
    "canvas": { "hwnd": 591100, "title": "动画组态窗口0" },
    "dialogs": [],                     // 非空 = 有模态框卡住
    "projects": [{
      "path": "D:\\MCGSPRO\\WORK\\新建工程0.MCP",
      "name": "新建工程0",
      "dirty": true,                 // 标题带 * = 有未保存修改
      "size": 761856,
      "modified": "2026-11-20T13:56:57",
      "readable": true,
      "control_count": 51,
      "windows": [
        { "name": "窗口0",   "control_count": 50, "blob_bytes": 13149 },
        { "name": "主控窗口", "control_count": 1,  "blob_bytes": 990 },
        { "name": "设备窗口", "control_count": 0,  "blob_bytes": 81 }
      ]
    }]
  }]
}
```

错误码：

| error_code | 含义 |
|---|---|
| `NO_MCGS_PROCESS` | 没有运行的 `McgsSetPro.exe` |
| `NO_PROJECT_OPEN_OPEN` | 进程在，但没探测到已打开的工程 |

两个字段是给后续写操作准备的前置条件：

- **`dialogs` 非空** —— MCGS 被模态框挡住（例如「格式错误」），此时任何写操作都不可靠，且该对话框关不掉，需重启进程
- **`dirty: true`** —— 有未保存修改，覆盖前应先备份 `.MCP`

### `mcgs_get_window`

读画面：一个窗口里所有控件的完整内容。

```jsonc
{
  "ok": true,
  "window": { "id": 1, "name": "窗口0", "width": 1920, "height": 1080, … },
  "control_count": 50,
  "kinds": { "label": 50 },
  "controls": [
    { "index": 0, "type_id": "0x5008", "kind": "label",
      "name": "控件98", "caption": "锡林MAX",
      "rect": [7, 3, 196, 62], "size": [189, 59],
      "fill": "#FFFFFF", "text_color": "#000000",
      "font": { "height": -20, "weight": 400, "face": "微软雅黑" } }
  ]
}
```

每个控件给出：

| 字段 | 含义 | 来源 |
|---|---|---|
| `index` | 在窗口里的序号 | 归档顺序 |
| `type_id` / `kind` | 控件类型（label / numeric_input / button / lamp …） | 类型 ID |
| `name` | 控件名（MCGS 里显示的「控件N」） | 公共头 |
| `rect` / `size` | 逻辑坐标与尺寸 | 公共头 |
| `fill` | 填充色；无填充为 `transparent` | 画刷 |
| `border` | 是否有边框 | 画笔 |
| `caption` | 显示文字 | 文字记录 |
| `text_color` | 文字颜色 | 文字记录 |
| `font` | 字号（负数=像素高）、字重、字体名 | LOGFONT |
| `unit` | 单位（非空时才出现） | 文字记录 |
| `payload_bytes` | 原始字节数，便于核对 | — |

`rect` 是**工程逻辑坐标**（从 `lbObjects` 二进制里解析），与屏幕分辨率无关。

**实测**：`窗口0` 的 50 个控件全部解析成功，0 错误；坐标与已知正确布局完全吻合（行 `3/62/121/180/239/298/357/416`，列 `7-196 / 346-535 / 685-874` 与 `207-346 / 546-685 / 885-1024`）。

带**动画**的控件（MCGS 的变量绑定）也支持：动画块是 MFC 序列化对象，长度不固定，所以用「从各偏移试解、要求刚好消耗到 payload 末尾」的方式定位真实起点。

### `mcgs_label_add` / `mcgs_control_update` / `mcgs_control_delete`

五个写工具共用一个流程：读出该页全部控件 → 在列表上增/改/删一项 → 清空画布 →
整页粘回 → 保存 → 跑组态检查。已有控件的 payload 原样保留，所以字体、动画、变量绑定都不受影响。

```jsonc
mcgs_label_add(window="针刺", caption="年", x=412, y=142,
               width=164, height=73, font_size=24,
               fill="#FFFF00", text_color="#FF0000", border_color="#0000FF")
```

返回重建后的完整控件列表和 `check_errors`：

```jsonc
{
  "ok": true, "window": "针刺", "control_count": 6,
  "controls": [ … ],              // 同 mcgs_get_window 的格式
  "check_errors": [],             // MCGS 组态检查的结果
  "dialogs_closed": [],           // 被自动关掉的对话框
  "backup": "…\新建工程0.20261121_110233.bak"
}
```

`index` 是 `mcgs_get_window` 返回的序号。`update` 只改传入的字段：只给 `x`/`y` 是平移，
只给 `width`/`height` 是从左上角缩放。

**每次写操作都会**：先备份 `.MCP`；把挡路的对话框自动关掉（优先点「取消」，
不会把属性面板里的内容应用上去）；保存后跑组态检查并把错误带回来。

**错误码**：`WINDOW_NOT_FOUND`、`INDEX_OUT_OF_RANGE`、`BAD_RECT`、`BAD_ARGUMENT`、
`BLOCKED_BY_DIALOG`、`CANVAS_NOT_OPEN`、`FORMAT_ERROR`、`COUNT_MISMATCH`、
`UNSUPPORTED_CONTROL`、`NO_MCGS_PROCESS`、`NO_PROJECT_OPEN_OPEN`

### 支持哪些控件类型

模板放在 `mcgs_max/templates.json`，每一个都是**在 MCGS 里手工放一个、再从工程数据库读回来的**，
所以带着该版本需要的全部内部结构（字体、边框、事件元数据、动画、内嵌图片）。

| type_id | 类名 | 控件 | 外观来源 | 能改 |
|---|---|---|---|---|
| 0x5008 | `CDrawLabel` | 文字标签 | 画笔/画刷 | 文字、坐标、尺寸、字号、字体、字重、字色、**填充、边框** |
| 0x5008 | `CDrawLabel` | 数值输出 | 画笔/画刷 | 同上 + 绑定变量与类型 |
| 0x5027 | `CDrawButton` | 按钮 | **内嵌 SVG/BMP** | 坐标、尺寸、按钮文字、文字字体 |
| 0x5028 | `CDrawMulBtn` | 开关 | **内嵌 BMP** | 坐标、尺寸、开/关文字、文字字体 |
| 0x502A | `CDrawMulDis` | 指示灯 | **内嵌 SVG/BMP** | 同上 |
| 0x5025 | `CDrawEdit` | 输入框 | **内嵌 BMP** | 坐标、尺寸 |

### 填充色和边框只有标签生效

实测：给指示灯设 `fill="#00FF00"`，字节确实改了，返回值也是 `#00FF00`，
但画布上还是原来的红色 —— **图片画在填充之上**。

| 控件 | 图片标记 | 填充/边框能渲染吗 |
|---|---|---|
| 文字标签 | 无 | ✅ 能 |
| 数值输出 | 无 | ✅ 能 |
| 按钮 | `<svg`×4, `BM`×2 | ❌ 不能 |
| 指示灯 | `<svg`×4, `BM`×2 | ❌ 不能 |
| 开关 | `BM`×2 | ❌ 不能 |
| 输入框 | `BM`×2 | ❌ 不能 |

工具在这种情况下会在返回值的 `warnings` 里说明，不会让你以为改成功了。

要换这几个控件的外观，得换模板或替换内嵌图片，不是设颜色字段。

模板都来自 MCGS 自带的东西：开关/指示灯/输入框是你在编辑器里放的那三个，
按钮取自 `D:\McgsPro\Samples\工程模版\模版_标准风格_冷色调_1024x600.mcp`。
| 0x5009 | `CDrawBitmap` | 位图 | ❌ 无模板 | — |
| 0x5027 | `CDrawButton` | 按钮 | ❌ 无模板 | — |
| 0x502D | `CDrawPic` | 实时曲线 | ❌ 无模板 | — |
| 0x5031/32/3D | `CDrawSliderFill` / `CDrawRotateTbl` / `CDrawDll` | 填充/仪表/报警 | ❌ 无模板 | — |

没有模板的类型仍然**能读、能原样保留、能跟着整页重建带过去**，只是不能从零创建。
要补某个类型：在 MCGS 里放一个，跑一次提取脚本写进 `templates.json` 即可。

**开关和指示灯的开关文字**可以读也可以改。这两段文字在 payload 里是三个连续的宽字符串：

```
文本0  |  按钮关  |  宋体      ← 关状态：对象名、显示文字、字体
文本0  |  按钮开  |  宋体      ← 开状态
```

定位靠这个结构，不靠文字内容，所以改过一次之后还能再改。

**输入框没有文本块**——画布上显示的「输入框」是 MCGS 的占位提示，不在数据里，改不了。

**按钮的 metadata 有嵌套长度**：metadata 块除了外层的长度前缀，内部还有两个 u16
（metadata+10、metadata+12）各自记录剩余字节数。只修外层时文字最多只能加 1 个字，
加 2 个字以上 MCGS 就拒绝粘贴；外层加内层一起修才能任意增长。

```
metadata 起始
  +0 .. +9   固定前缀
  +10        u16 = 3539      ← 3539 + 12 = metadata 长度
  +12        u16 = 3535      ← 3535 + 16 = metadata 长度
  ...
  +86        标题（宽字符串）
```

判别方式：若 `u16@+10 + 12 == metadata 长度`，就认定存在嵌套长度并一并修正；
开关、指示灯的 metadata 只有 12 字节，不会误判。

**按钮的两个特殊之处**：

1. 按钮文字在 payload 里存了**两份**（metadata 内一份、metadata 之后一份），
   只改一份 MCGS 就拒绝粘贴。而且 metadata 带长度前缀，改了里面的文字必须同步改长度。
   `builder.replace_global()` 负责这两件事。
2. 按钮模板自带一个「打开窗口 主页」的动作，指向示例工程里的窗口。
   我们的工程没有这个窗口，组态检查会报 `未知窗口名`，所以构造按钮时会把该引用清空
   （`builder.set_action_window`）。需要动作时传 `action_window="窗口名"` 指到真实窗口。

**按钮的动作变量能读**：`CAniClick` 的表达式固定在类标记之后 **53 字节**处。

```
CAniClick 类标记(15B) | float(4) | 空串(4) | u16 串数=8 | 7 个空串(28) | 变量名
                                                                        ↑ +53
```

偏移不是算出来的，是扫出来的：在 MCGS 公共图库的 228 个含 `CAniClick` 的元件里，
逐个从类标记后找第一个非空宽字符串，227 个落在同一偏移（+53），1 个为空。

`mcgs_get_window` 会把它作为 `action_variable` 返回。数据来自 MCGS 公共图库
（`D:\McgsPro\Program\library\public\公共图库.lib`，本身也是 Jet 数据库，1021 个元件）。

**改动作变量**：`mcgs_control_update(..., action_variable="@新变量")` 可以给**已有动作**的按钮换目标变量。
做法是在 `CAniClick` 的表达式位置原地替换宽字符串，字节级往返在图库 228 个元件上验证过 681 次全对。

**加动作做不到**：试过把图库里的 143 字节 `CAniClick` 块插进按钮模板并粘贴，
MCGS 直接拒绝（页面自动从备份恢复）。块尾那 78 字节在 228 个元件里有 12 种变体，
推测含流相关的引用，照搬无效。要加动作请先在 MCGS 里手动加一个，之后就能用工具改变量。

### 按钮动作的 5 种模式

MCGS 的「动画按钮构件属性设置」里，`功能` 有 5 个单选项。用程序逐个设置、保存、读回，
定位到两个字段：

| 模式 | 模式字 (u32) | 标志位 (u16) |
|---|---|---|
| 置1 | 0 | 0x8000 |
| 置0 | 1 | 0x8000 |
| 取反 | 2 | 0x8000 |
| 按1松0 | 3 | 0x8004 |
| 按0松1 | 4 | 0x8004 |

**模式字的位置**：`1.0f` 紧跟 `100.0f` 这 8 个字节在每条 动画按钮 记录里只出现一次，
模式字就在它前面 4 字节处。不用绝对偏移，因为字符串长度会变。

**标志位**在公共头里（动画计数之后的第二个 u16），第 2 位表示「按下/松开」语义，
三种「置/取反」是 0x8000，两种「按X松Y」是 0x8004。两个字段必须一起改。

用法：

```jsonc
mcgs_control_update(window="参数", index=0, action_mode="取反")
```

`mcgs_get_window` 会返回 `action_mode`。

**验证方式**：用工具设好模式 → 保存 → 打开 MCGS 的属性对话框 → 读 5 个单选项的勾选状态。
5 种模式逐一验证，全部一致。

### 输入框的数值格式

`mcgs_control_update` 支持这些参数，只对输入框（0x5025）有效：

| 参数 | 含义 |
|---|---|
| `decimals` | 固定小数位数（0..8），**会自动把「自然小数」勾去掉** |
| `natural_decimals` | 是否用自然小数 |
| `min_integer_digits` | 最小整数位数 |
| `unit` | 单位文字，可长可短 |
| `unit_enabled` | 是否显示单位（对应「单位」勾选框） |
| `minimum` / `maximum` | 取值范围 |

```jsonc
mcgs_control_update(window="故障", index=0,
                    decimals=4, min_integer_digits=3,
                    unit="V", unit_enabled=True,
                    minimum=-20.0, maximum=80.0)
```

**关键：这条记录把单位周围的格式字存了两份，MCGS 读的是第二份。**
只写第一份会被静默忽略——文件改了，MCGS 里没变。

```
单位(副本1) | 格式字 ×4 ... 文字 ... 单位(副本2) | 格式字 ×4
                              ↑ 真正生效的是这一份
```

相对单位字符串的固定偏移：

```
unit_start - 12   u32  小数位模式
unit_start -  8   u32  常量 0xFFFFFFFF
unit_end   +  0   u32  单位是否显示
unit_end   +  4   u32  常量 1
unit_end   +  8   u32  最小整数位数
unit_end   + 12   u32  固定小数位数
文字结束   +  0   f32  最大值
文字结束   +  4   f32  最小值
```

单位字符串在 metadata 内部，改长度要同时修正外层长度前缀和两个嵌套长度，
和按钮文字是同一套处理（`replace_cstring_at`）。

**验证方式**：工具设完后打开 MCGS 的「输入框构件属性设置」，逐个读字段。
设 4 位小数 / 3 位整数 / 单位 V / -20..80 后，对话框显示 `4` / `3` / `V` / `-20` / `80`，
「自然小数」未勾选、「单位」已勾选。

**已知不足**：`natural_decimals` 的读回不可靠——MCGS 有时把小数位写成 0、有时写成 -1。
写入本身是对的（对话框确认过），只是读回来不能当准。

**变量连接还做不到**：输入框的数据对象入口是「对应数据对象的名称」按钮弹出的选择框，
输入框里那个框是只读显示。而且当前工程 `Data` 表是空的，没有变量可绑。

**还改不了的**：开关/指示灯的亮灭颜色（在内嵌图片里）。

### 读不出来的东西

这些字段在 payload 里存在，但含义尚未验证，因此**不输出**，避免给出错值：

| 未解码 | 说明 |
|---|---|
| 边框颜色 / 线宽 | 所有控件共用同一个 16 字节画笔块，样本里完全相同，无法区分各字段含义，只输出「有没有边框」 |
| 对齐方式 / 小数位数 / 显示格式 | 在文字记录的外观前缀和后缀里，未逐位验证 |
| 变量绑定（动画） | 动画块只用于定位文字，内容本身未解码——**所以看不到控件绑了哪个变量** |
| 按钮行为 / 输入框范围 | 未解码 |

## 实现要点

**工程路径怎么来的**：MCGS 的「工作台」窗口标题是 `工作台 : <工程路径>`，未保存时结尾多一个 `*`。代码用正则提取，并要求结果**形如真实路径**（盘符 / UNC / 以 `.MCP` 结尾），否则跳过——工作台工具条的标题也含「工作台」字样，不能误认。

**工程文件怎么读的**：`.MCP` 是 Microsoft Access/Jet 数据库。MCGS 运行时持有该文件，所以先复制到临时目录再解析，读完即删，不干扰正在运行的编辑器。

**控件数量怎么算的**：`WndUser` / `WndControl` / `WndDevice` 表的 `lbObjects` 字段是一个 `MCGS-SET DOCUMENT` 归档流：

```
12 字节头 | "MCGS-SET DOCUMENT;VERSION 01.00"
u32 对象数 | 每个对象: u32 类型 + 长度 + payload
```

按这个结构遍历即可数出控件，并顺带取出每个控件的矩形坐标（payload 头部偏移 12 起是 `4×int32`）。

**为什么不用截图**：截图受窗口尺寸、缩放、滚动位置影响，像素差异无法证明坐标正确。工具一律返回**从工程数据里读出的数字**。

**为什么全程不用坐标**：所有控件操作都是「按类名/标题找到 HWND，再 `PostMessage` 一个标准控件消息」。把 MCGS 主窗口从 `(-8,-8) 2568×1292` 挪到 `(40,30) 1200×800` 后创建窗口依然成功——位置、尺寸、分辨率都不参与。

**画布怎么打开**：列表上按 Enter 没用，工作台的「动画组态」按钮点了也没反应，只有双击列表项才真的打开画布。所以 open_canvas 用 LVM_GETITEMPOSITION 取行位置，再投递一次双击。

**边框颜色在哪**：画笔块是 u16 样式 + 4 个 u32。用一个标签改一个字段、贴到空窗口截图对比，才确定下来：

| 偏移 | 含义 | 依据 |
|---|---|---|
| +0 | 画笔样式（6 = 默认边框） | 改成 0 边框消失 |
| +4 | 线宽（默认 2） | 改成 255 时黑边糊满整个框 |
| +8 | 未观察到作用 | 改成红色无变化 |
| +12 | 边框颜色 COLORREF | 改成红色边框变红 |

**对话框怎么处理**：写操作前先自动关掉挡路的对话框，按「取消 → 否 → 关闭 → 确定」的顺序找按钮。
优先「取消」是因为属性面板点「确定」会把面板里的内容应用到控件上，那不是调用方要的结果。
关不掉就返回 BLOCKED_BY_DIALOG 并列出标题，不硬来。

**为什么必须整页重建**：MCGS 对已有内容的画布**静默忽略**粘贴，追加不生效。所以每次写操作都是清空 + 整页粘回。已有控件的 payload 字节不动，所以动画和变量绑定都保住。

**类槽位不能用引用**：MFC 的类注册表按出现顺序分配槽位，`0x8000|槽号` 是引用形式。
一开始我用引用压缩流，只算外层控件类——但非文字控件的 payload 内部还会注册 `CDrawCell`
（开关、灯、输入框各 2 个），把槽位顶掉，于是第二个控件引用到错的类，MCGS 静默拒绝，整页变空。
现在**每个控件都写完整类标记**，不做任何槽位运算。重复注册 MCGS 是接受的。

**整页重建会改变 payload 长度**：重建后 MCGS 会重新序列化，同样内容的控件可能比原来少 2 字节。
几何、类型、顺序都不变，组态检查也是 0 错误，属于 MCGS 自己的规范化。

**开关/指示灯的文字怎么找的**：把 payload 里所有宽字符串按顺序列出来，`文本0` 后面紧跟的就是显示文字，再后面是字体名。这样定位与文字内容无关，改名之后仍然有效。

**输入框的「输入框」不是数据**：全 payload 里搜不到这个字符串，它是 MCGS 对空输入框的占位渲染。

**粘贴失败会回滚**：控件数对不上时，`page.replace_page` 会自动把备份恢复回去并重启 MCGS，
而不是把清空的页面留在磁盘上。这一步曾经弄丢过一次用户的控件。

**自检脚本**：`selftest.py` 跑一遍覆盖全部工具、四类控件、混合页面、边界值和参数错用，
在空窗口 `参数` 上做，跑完清空；另外读一遍 MCGS 公共图库验证动作变量偏移。46 项全绿才算通过。

## 多工程：用 project 选

MCGS 可能同时开着多个工程（多个实例，或一个实例里多个工作台）。5 个窗口级工具都接受一个
可选的 `project` 参数：

```jsonc
mcgs_get_window(window="针刺", project="新建工程0")
mcgs_control_add(window="针刺", kind="button", x=10, y=10,
                 width=100, height=40, project="D:/MCGSPRO/WORK/另一个.MCP")
```

五个窗口级工具的参数名是统一的：`window` 是窗口名，`project` 是工程，两个都必填。

```jsonc
mcgs_get_window(window="针刺", project="新建工程0")
```

匹配顺序：

1. **完整路径**（大小写、正反斜杠都不敏感）
2. **工程名**（不含 `.MCP` 扩展名）
3. **唯一片段** —— 路径或名字里包含这段文字，且只匹配到一个

**`project` 是必填参数，没有默认值。** 不给会被 schema 挡下，给空串返回：

```json
{
  "ok": false,
  "error_code": "PROJECT_REQUIRED",
  "message": "必须用 project 指定工程。当前打开：新建工程0、新建工程3。",
  "available": [
    {"name": "新建工程0", "path": "D:\\MCGSPRO\\WORK\\新建工程0.MCP"},
    {"name": "新建工程3", "path": "D:\\MCGSPRO\\WORK\\新建工程3.MCP"}
  ]
}
```

这是故意的。早先的版本是「用枚举到的第一个」，但进程枚举顺序不稳定——用户切一下窗口，
顺序就可能变，写操作会悄悄落到另一个工程里。工具不该靠猜。

匹配不到返回 `PROJECT_NOT_FOUND` 并列出当前打开的工程；匹配到多个返回 `PROJECT_AMBIGUOUS`
并列出候选，此时要写全名或完整路径。

工程与进程是一起解析的：`resolve_project()` 会遍历所有 `McgsSetPro.exe` 实例的所有工作台，
所以同时开两个 MCGS 也能分别操作。

## 运行时行为

### 打开画布：优先不碰鼠标

`open_canvas` 先给列表项发双击消息，不动光标。MCGS 窗口不是活动窗口时会忽略这些消息，
这时才退回真实鼠标双击，**并立即把光标放回原处**。

```
1. 发 WM_LBUTTONDBLCLK      → 不动鼠标
2. 等 5 秒没反应
3. 真实双击（SetCursorPos + mouse_event）→ 完事立刻还原光标位置
```

所以常态下不干扰，只在必要时短暂借用一下光标。

### 备份与文件大小

每次写入前复制一份工程，**只保留最近 1 个**，用于失败回滚。

工程文件在一次会话内会随写入次数增长（每次约等于该窗口的数据大小），**重启 MCGS 后
自动压缩回去**，内容大小不变，不影响下载。长时间连续写入时注意磁盘余量。

### 运行时不会干扰你

工具**不碰鼠标键盘**——只用 `SendMessageW` 给指定窗口句柄发消息，
没有 `SetCursorPos` / `mouse_event` / `SendInput` / `keybd_event`。
粘贴是给画布发菜单命令，不是模拟 Ctrl+V。

**唯一副作用：会清空剪贴板。** 写操作要往剪贴板放 MCGS 私有格式，
`EmptyClipboard()` 之后再写入，原内容不保存也不恢复。

另外工具假设 MCGS 已经在前台、画布窗口开着。MCGS 最小化时 `PostMessage` 可能不生效。

## 自检

```powershell
<python路径> D:\mcgs-max\selftest.py
```

自检在空窗口 `铺网` 上做，跑完清空。**窗口非空时拒绝运行**，不会误删内容。

覆盖：协议、读工具、四类控件增删改、混合页面、参数错用、外观生效范围、
按钮动作模式、输入框数值格式、工程选择、图库动作变量。

或手工验证 stdio：

```powershell
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | <python路径> D:\mcgs-max\run.py
```

stdout 只输出协议消息，日志全部走 stderr。
