# MCGS 组态自动化知识库

配套 `mcgs-max` MCP 使用。本文档说明**用这套工具干活的正确方法**。

> **一律通过 `mcgs_*` MCP 工具操作，不要直接修改 `.MCP` 文件。**
> 所有写入走「整页重建」：读取全部控件 → 修改 → 清空画布 → 粘贴 → 保存 → 组态检查。
> 工具内部负责长度字段修正、失败回滚和组态检查；绕过工具这些都没有。

---

## 1. 能力范围

| 能力 | 支持 |
|---|---|
| 读工程、窗口、控件 | ✅ |
| 读 PLC 设备与通道表 | ✅ |
| 增删控件 | ✅ |
| 改文字、位置、尺寸、字体、颜色 | ✅ |
| 绑定 / 改绑 PLC 变量 | ✅ |
| 改输入框数值格式 | ✅ |
| 改开关动作模式 | ✅ |

---

## 2. 工具参考

| 工具 | 必填参数 | 用途 |
|---|---|---|
| `mcgs_get_project` | — | 列出所有打开的工程 |
| `mcgs_get_device` | `project` | 读 PLC 设备与通道表 |
| `mcgs_get_window` | `window` `project` | 读窗口内全部控件 |
| `mcgs_label_add` | `window` `caption` `x` `y` `width` `height` `project` | 新增文字标签 |
| `mcgs_control_add` | `window` `kind` `x` `y` `width` `height` `project` | 新增按钮/开关/灯/输入框 |
| `mcgs_control_update` | `window` `index` `project` | 修改一个控件 |
| `mcgs_controls_update` | `window` `updates` `project` | 批量修改，一次重建 |
| `mcgs_control_delete` | `window` `index` `project` | 删除一个控件 |

### 参数约定

`project` 与 `window` 均为必填。`project` 接受完整路径、工程名或能唯一匹配的片段。

工程名匹配到多个时返回 `PROJECT_AMBIGUOUS` 并给出候选，此时使用完整路径。

---

## 3. 控件类型

| type_id | 类名 | 名称 | 外观来源 |
|---|---|---|---|
| `0x5008` | `CDrawLabel` | 文字标签 / 数值输出 | 画笔画刷 |
| `0x5025` | `CDrawEdit` | 数值输入框 | 内嵌位图 |
| `0x5027` | `CDrawButton` | 标准按钮 | 内嵌矢量图/位图 |
| `0x5028` | `CDrawMulBtn` | 开关（属性框标题为「动画按钮」） | 内嵌位图 |
| `0x502A` | `CDrawMulDis` | 指示灯 | 内嵌矢量图/位图 |
| `0x503D` | `CDrawDll` | 外挂构件 | 未解析 |

`0x5027` 与 `0x5028` 是两种不同控件：前者是标准按钮，后者是开关。
开关的 5 种动作模式只属于 `0x5028`。

### 各类型可修改的属性

| 控件 | 文字 | 字体 | 坐标尺寸 | 填充/边框 | 变量 |
|---|---|---|---|---|---|
| 文字标签 | ✅ | ✅ | ✅ | ✅ | ✅（输出型） |
| 数值输入 | — | — | ✅ | ⚠️ | ✅ |
| 标准按钮 | ✅ | ✅ | ✅ | ⚠️ | 改绑 |
| 开关 | ✅ 双态 | ✅ | ✅ | ⚠️ | ✅ |
| 指示灯 | ✅ 双态 | ✅ | ✅ | ⚠️ | ✅ |

⚠️ 标记的类型以外观由内嵌图片绘制，`fill` / `border_color` 会写入数据但不影响显示。
此类调用会在返回值的 `warnings` 中说明。

---

## 4. 填写变量

### 标准流程

```
① mcgs_get_window(window, project)
   取得每个控件的 index / kind / rect / caption / variable

② mcgs_get_device(project)
   取得全部可用通道名

③ 确定每个控件应绑定的通道

④ mcgs_controls_update(window, updates=[{index, variable}, …], project)
   一次调用完成全部修改

⑤ 重新 mcgs_get_window，逐个核对 variable 字段
```

### 变量名格式

```
<设备名>_<通道名>

设备0_读写DWUB1000
设备0_只读X0
设备0_读写M1
```

设备名与通道名均取自 `mcgs_get_device` 的返回结果，不要自行拼写。

传入不存在的通道名返回 `UNKNOWN_VARIABLE`，并在 `available` 中给出可用通道。

### 通道在控件中的存储位置

| 类型 | 位置 | 副本数 |
|---|---|---|
| 数值输入 | 字体串前两个 | 1 |
| 指示灯 | 字符串表倒数第 2 项 | 1 |
| 开关 | 最后一个字体串之后 | 2 |

开关与输入框的字符串表项数随控件配置变化，位置不固定。

**工具按内容定位**：扫描控件的全部宽字符串，识别形如 `<设备名>_<通道名>` 的项。
改绑时替换全部副本。

### 从无到有绑定

支持数值输入、指示灯、开关。标准按钮与文字标签不支持，返回 `UNSUPPORTED_CONTROL`。

未绑定的控件有两种字符串表形状：新建时 14 项，配置后 10 项。槽位按「字体串前两个」定位，
两种形状都适用。

传空字符串表示解绑。

**首次绑定会重置数值格式。** MCGS 在控件从「未绑定」变成「已绑定」时，会把小数位、单位、
取值范围恢复成默认值。因此：

- 给已绑定的控件改绑 —— 格式保留，一次调用即可
- 给未绑定的控件首次绑定 —— 先绑定，保存后再单独设一次格式

工具在这种情况下会在 warnings 里提示。

### 按钮动作

标准按钮的动作是一段多行脚本，不是单个通道：

```
设备0_读写DWUB1001=设备0_读写DWUB1001*1.01
设备0_读写DWUB1003=设备0_读写DWUB1003*1.01
设备0_读写DWUB1005=设备0_读写DWUB1005*1.01
```

**改绑按钮 = 替换整段脚本。** 先读出原脚本，替换其中的通道名，再整段写回：

```jsonc
// 1. mcgs_get_window 读到 variable 就是这段脚本
// 2. 把里面的通道名换掉
// 3. 整段写回
mcgs_control_update(window="针刺", index=124, project="新建工程0",
                    variable="设备0_读写DWUB500=设备0_读写DWUB500*1.01\n…")
```

规则：

- 传**单个通道名**会被拒绝（会覆盖整段脚本）
- 传**空字符串**会被拒绝（会删除整段动作）
- 脚本内容不按设备通道表校验，按「含 `=` 或换行」判定

不支持为无动作的按钮新建脚本。

### 映射关系如何确定

两种来源：

- **从画面推断** —— 标签文字中常含寄存器编号（如 `1#气压X0`），据此确定相邻控件的通道。
  采用前先抽查数个控件确认规律成立。
- **用户提供对照表** —— 标签命名不规整时使用。

---

## 5. 输入框数值格式

```jsonc
mcgs_control_update(
  window="针刺", index=0, project="新建工程0",
  decimals=2,                 // 固定小数位，同时取消「自然小数」
  min_integer_digits=1,
  unit="S", unit_enabled=True,
  minimum=0, maximum=999
)
```

### 字段位置

```
0xFFFFFFFF | u32 | 单位串 | 单位是否显示 | u32 | u32
   哨兵                      +8            +12
                        最小整数位      小数位
```

每条记录包含两个这样的结构。工具写入全部结构。

定位方式：先找 12 字节前带 `0xFFFFFFFF` 的单位串，逐一校验单位是否显示为 0/1、
最小整数位 0..8、小数位为 -1 或 0..8。MCGS 规范化已绑定的控件时会清掉这个哨兵值，
此时退回按结构识别（同上三项校验，并排除紧跟在字体串后面的那个空串）。

### 自然小数

`decimals=N` 写入固定小数位并置模式为「固定」，对应属性框「自然小数」不勾选。
`natural_decimals=true` 写入 -1。

`natural_decimals` 的读回值不作为判断依据，以 `decimals` 字段为准。

---

## 6. 开关动作模式

```jsonc
mcgs_control_update(window="针刺", index=40, project="新建工程0", action_mode="置1")
```

| 模式 | 模式字 | 标志位 |
|---|---|---|
| 置1 | 0 | 0x8000 |
| 置0 | 1 | 0x8000 |
| 取反 | 2 | 0x8000 |
| 按1松0 | 3 | 0x8004 |
| 按0松1 | 4 | 0x8004 |

仅适用于 `0x5028`。标准按钮（`0x5027`）无此字段。

---

## 7. 批量写入

每次写入都重建整个页面：读取全部控件 → 清空画布 → 写入剪贴板 → 粘贴 → 保存 → 组态检查。
耗时随窗口大小增长。

| 窗口大小 | 单次重建耗时 |
|---|---|
| 数十 KB | 约 10 秒 |
| 7 MB / 132 控件 | 约 13 秒 |

同一页面的多处修改合并为一次 `mcgs_controls_update` 调用。
修改前先看 `mcgs_get_window` 返回的 `blob_bytes`。

---

## 8. 返回值解读

### 控件对象

```jsonc
{
  "index": 0,
  "type_id": "0x5025",
  "kind": "numeric_input",
  "name": "控件396",
  "rect": [90, 417, 236, 475],
  "size": [146, 58],
  "fill": "#C0C0C0",
  "variable": "设备0_读写DWUB1000",
  "number_format": { "decimals": 2, "unit": "", "minimum": 0, "maximum": 50 }
}
```

`index` 为控件的操作句柄。修改属性不改变 `index`；新增或删除控件后，其后控件的 `index` 前移，
需重新读取。

`name` 由 MCGS 在每次粘贴时重新分配，不能作为控件标识。

### 写入结果

写入类工具返回：

| 字段 | 含义 |
|---|---|
| `ok` | 组态检查是否无错误 |
| `controls` | **保存后重新读回**的控件内容 |
| `check_errors` | MCGS 组态检查的错误列表 |
| `warnings` | 不影响数据但影响显示的情况 |
| `backup` | 本次写入前的工程备份路径 |

**以 `controls` 中的实际字段值确认修改结果。** `ok=true` 表示组态检查无错误，
不代表每一项修改都被 MCGS 接受；未被接受的字段在 `controls` 中保持原值。

---

## 9. 运行环境

| 要求 | 说明 |
|---|---|
| Windows | 使用 Win32 API |
| 已安装 McgsPro | 操作正在运行的组态环境 |
| Python 3.9 以上 | 64 位 |
| MCGS 中已打开工程 | 否则返回 `NO_PROJECT_OPEN` |

无需安装额外依赖（第三方库已随包提供）、无需注册 COM 组件、无需管理员权限、不占用端口。

### 备份与文件大小

每次写入前会把整个工程复制一份，只保留**最近 1 个**（`工程名.YYYYMMDD_HHMMSS.bak`），
用于写入失败时回滚。MCGS 自己也维护 `工程名.MCP.bak` 和 `.MCP.user.bak`，不受影响。

**工程文件在一次会话内会增长，重启 MCGS 后自动压缩回去。** 实测：

```
起点      7.44 MB
写一次   14.16 MB   (+6.7)
再写一次  20.88 MB   (+6.7)
重启后    7.42 MB   ← MCGS 压缩
```

原因是 MCGS 保存时更新整个窗口的 blob，Jet 释放旧页但不归还文件。内容大小始终不变，
空闲页不属于任何记录，所以**不影响下载到触摸屏**，也不必手动压缩——重启即可。

长时间连续写入时文件会临时变大，注意磁盘余量。

### 对系统的占用

工具通过向指定窗口发送消息完成操作，不模拟鼠标键盘输入。

打开画布时优先发送消息；若工作台窗口非活动状态导致消息被忽略，则使用真实鼠标双击，
并在操作后立即恢复光标位置。

写操作会清空系统剪贴板并写入 MCGS 私有格式，原内容不保留。

---

## 10. 配方

### 配方 A：为整页控件绑定变量

```
1. mcgs_get_window(window, project)
2. mcgs_get_device(project)
3. 依据标签文字或用户提供的对照表确定映射
4. mcgs_controls_update(window, updates, project)
5. mcgs_get_window 核对 variable 字段
```

### 配方 B：统一调整一页的数值格式

```
1. mcgs_get_window，筛出 kind == "numeric_input" 的控件
2. 按控件上方标签或坐标位置分类
3. mcgs_controls_update 一次提交
4. mcgs_get_window 核对 number_format
```

### 配方 C：结果核对

```
mcgs_get_window 后逐个比对 variable / number_format / texts 等目标字段
```

写入工具返回的 `controls` 已包含保存后的实际内容，可直接用于核对。

---

## 11. 错误码

| 错误码 | 含义 | 处理 |
|---|---|---|
| `NO_MCGS_PROCESS` | 未运行 McgsPro | 启动组态环境 |
| `NO_PROJECT_OPEN` | 未打开工程 | 在 MCGS 中打开工程 |
| `PROJECT_REQUIRED` | 未指定 `project` | 传工程名或路径 |
| `PROJECT_NOT_FOUND` | 工程未打开 | 查看 `available` |
| `PROJECT_AMBIGUOUS` | 片段匹配多个工程 | 使用完整路径 |
| `WINDOW_NOT_FOUND` | 窗口名不存在 | 查看 `available` |
| `INDEX_OUT_OF_RANGE` | 控件序号越界 | 重新读取窗口 |
| `UNKNOWN_VARIABLE` | 通道名不存在 | 用 `mcgs_get_device` 取准确名称 |
| `UNSUPPORTED_CONTROL` | 该控件不支持此操作 | 见第 3、4 节 |
| `BAD_ARGUMENT` | 参数值非法 | 按提示修正 |
| `BAD_RECT` | 宽高或矩形不合法 | 宽高需大于 0 |
| `CANVAS_NOT_OPEN` | 无法打开窗口画布 | 确认窗口存在且 MCGS 非最小化 |
| `BLOCKED_BY_DIALOG` | 有对话框无法关闭 | 手动关闭后重试 |
| `COUNT_MISMATCH` | 粘贴未成功 | 工具已自动从备份恢复；检查控件内容是否合法 |
| `FORMAT_ERROR` | MCGS 拒绝了这个控件流 | 同上 |
| `WINDOW_LOST` | 保存后找不到窗口 | 重新读取工程 |
| `PARSE_FAILED` | 控件数据解析失败 | 该控件可能由更新的 MCGS 版本创建 |
| `NO_DEVICE` | 工程未配置 PLC 设备 | 绑定变量前先配设备 |
| `DEVICE_READ_FAILED` | 设备窗口读取异常 | 检查工程是否损坏 |
| `NO_MAIN_WINDOW` | 找不到 MCGS 主窗口 | 确认组态环境已启动 |
| `NO_WORKBENCH` | 找不到工作台窗口 | 确认工程已打开 |
| `WINDOW_DRIVE_FAILED` | 窗口列表操作失败 | 重试；仍失败则重启 MCGS |

---

## 12. 当前不支持

| 项 | 说明 |
|---|---|
| 为无变量的标准按钮、文字标签新建绑定 | 槽位未确认 |
| 为无动作的标准按钮新建脚本动作 | 同上 |
| 开关、指示灯的亮灭颜色 | 由内嵌图片决定，无对应字段 |
| 输入框除数值格式外的其他选项 | 字段未定位 |
| `0x503D` 外挂构件的属性 | 类结构未解析 |
| 新建、删除、重命名窗口 | 无法通过剪贴板操作完成 |

---

## 13. 执行顺序

1. 调用 `mcgs_get_project` 确认目标工程已打开
2. 调用 `mcgs_get_window` 与 `mcgs_get_device` 取得现状
3. 规划修改，同一页面合并为一次批量写入
4. 提交写入
5. 依据返回的 `controls` 核对结果
6. 需要时用 `backup` 中的备份恢复
