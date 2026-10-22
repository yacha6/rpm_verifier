# LLM API 速率验证工具

以指定的速率(RPM)向 OpenAI 兼容 API 重复发请求,测量实际达成的速率,并统计各 HTTP 状态码的出现次数。支持六种模态:**文本、图像、视频、图生图、图生视频、视频生视频**。

产品定位是**验证器,不是探测器**:你填入一个假设值(「我这个 key 应该能跑 100 RPM」),工具按这个值跑一遍,然后如实报告发生了什么 —— 不自动调速、不给达标结论。

完整需求见项目根目录的 `prd-llm-api-rpm-verifier-v1.1.md`(仓库里包含;源码分发包里不含)。v1.0 的纯文本版本保留在 `prd-llm-api-rpm-verifier.md`。

## 从 GitHub 拉下来

```bash
git clone <仓库地址>
cd rpm_verifier

# macOS / Linux
python3 launcher.py

# Windows(PowerShell / cmd)
py -3.12 launcher.py
```

`launcher.py` 会自己建 `.venv`、装依赖、启动界面。第一次需要下载界面库
(约 110MB),之后启动只要几秒。也可以直接双击 `启动.command`(macOS)或
`启动.bat`(Windows)。

**不要提交或拷贝 `.venv`。** 它是平台相关的二进制目录,仓库里的
`.gitignore` 已经排除了它。`git add .` 不会把它带进去;如果你之前已经
`git add -f .venv`,先 `git rm -r --cached .venv` 再提交。

| 系统 | 前置条件 | 备注 |
|---|---|---|
| macOS | Python 3.10–3.14 | 首次双击 `.command` 可能被 Gatekeeper 拦,按下面的 `xattr` 命令处理 |
| Windows | Python 3.10–3.14,安装时勾选 **Add python.exe to PATH** | `.bat` 会自己找 `py -3.12` / `python` |
| Linux | Python 3.10–3.14 + **图形桌面环境** | 纯 SSH 无显示器跑不了 Qt 界面;缺库见下方 |

Linux 上如果 Qt 报 `could not load the Qt platform plugin "xcb"`,按发行版补系统库
(Ubuntu / Debian):

```bash
sudo apt install libxcb-cursor0 libxkbcommon-x11-0
# 中文缺字的话
sudo apt install fonts-noto-cjk
```

macOS 首次双击 `.command` 如果提示「来自身份不明的开发者」,在项目目录执行一次:

```bash
xattr -d com.apple.quarantine 启动.command
chmod +x 启动.command
```

> 目前只在 macOS 上实际跑过完整界面和测试;Windows / Linux 的启动器代码是
> 跨平台的,但建议第一次在目标机器上跑一遍 `python3 launcher.py --selftest`,
> 确认依赖和 offscreen 测试都通过。

## 快速开始

**如果你只是要用这个工具**,别往下看了,直接双击 `启动.bat`(Windows)或
`启动.command`(macOS)。启动器会自己建虚拟环境、装依赖、开界面,几分钟搞定。

下面是给要改代码的人看的。

### 已经建好环境的情况

```bash
# macOS / Linux
.venv/bin/python -m rpm_verifier.app

# Windows
.venv\Scripts\python.exe -m rpm_verifier.app
```

### 从零搭环境

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Windows(PowerShell / cmd)
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

需要 Python **3.10 ~ 3.14**。这个区间是 PySide6 / PySide6-Essentials 的
元数据写死的(`Requires-Python: <3.15,>=3.10`),两头都越不过去。

### ⚠️ 不要把 `.venv` 拷到别的机器或别的系统

虚拟环境里装的是**平台相关的二进制**(`.venv` 里有 Mach-O / PE 可执行文件,
整个目录 1.2G)。在 macOS 上建的 `.venv` 拿到 Windows 上,里面的
`python.exe` 根本不存在,只会得到一句莫名其妙的「找不到文件」。

换机器就删掉 `.venv` 重新建 —— 也就是重跑一次 `启动.bat` / `启动.command`,
或者上面那两条命令。分发包里**不含** `.venv`,正是这个原因。

## 用法

1. 填 Base URL(缺 `/v1` 会自动补全)、API Key、模型名
2. 选**模态**(文本 / 图像 / 视频 / 图生图 / 图生视频 / 视频生视频),按需调该模态的参数
3. 输入条件模态填**源素材**:图生图支持公开图片 URL 或本地图片;图生视频/视频生视频只支持公开 URL
4. 填目标 RPM 与测试时长(默认 60 秒)
5. 点「开始测试」

测试中所有配置项会锁定,结束后展示实际 RPM 与状态码统计表。

配置卡片下方的「请求地址」会实时显示最终拼出来的完整端点。Base URL 填错
时,这是最快能看出问题的地方。

## 六种模态

| 模态 | 端点 | 响应形态 | 请求超时 | 默认参数 |
|---|---|---|---|---|
| 文本 | `POST /chat/completions` | 同步 | 30s | `max_tokens=1` |
| 图像 | `POST /images/generations` | **同步**(生成完才回) | 120s | `size=1K` |
| 视频 | `POST /videos` | **异步**(立刻返回排队任务) | 30s | `mode=text`, `seconds=4`, `size=720P` |
| 图生图 | `POST /images/generations` | **同步** | 120s | `size=1K`, `extra_body.image=[素材]` |
| 图生视频 | `POST /videos` | **异步** | 30s | `mode=keyframe`, `first_frame=素材`, `seconds=4`, `size=720P` |
| 视频生视频 | `POST /videos` | **异步** | 30s | `mode=reference`, `videos=[素材]`, `seconds=4`, `size=720P` |

⚠️ 视频的 `mode` 是**必填**项,漏了直接 400。文档的参数表里标着「是」,
但很容易看漏。文生视频固定用 `text`;图生视频用 `keyframe`;视频生视频用
`reference`。

### 源素材怎么填

| 模态 | 公开 URL | 本地文件 | 说明 |
|---|---|---|---|
| 图生图 | ✅ | ✅ | 本地 JPG/PNG/WebP,≤ 5 MB;编码成 Data URI 随请求发送 |
| 图生视频 | ✅ | ❌ | 服务端要自行下载图片,只认公开 URL |
| 视频生视频 | ✅ | ❌ | 服务端要自行下载视频,只认公开 URL;2–12 秒、24–60 FPS、< 50 MB |

**为什么图生视频/视频生视频不能用本地文件**:这两个接口由服务端去拉素材,
文档要求素材「可由服务公开访问」。工具没有上传接口,也没有素材托管服务;
把本地路径塞进 JSON 只会在服务端 400。与其让人猜,不如在界面直接说清楚。

**源素材不落盘。** 签名 URL 的 query 里通常带着临时凭据,不能写进
`config.json`;本地图片的 Data URI 也只驻留内存。界面摘要和报告只显示
「图片 URL(cdn.example.com/cat.png)」这种脱敏信息,完整 URL 只留在输入框里
供你编辑。

**素材是固定输入。** 一次测试只用一个源素材,所有请求复用它。工具不会随机
换图 —— 否则 RPM 的变化就分不清是限流还是素材差异。

**参数默认值一律取最低成本档。** 图像按张、视频按秒计费,默认值选错会让一次
压测烧掉几十美元。界面上可以改,但默认必须是便宜的。

**视频测的是「创建任务」这一步的限流,不是视频生成的吞吐。** 异步接口只负责
接单,生成在服务端后台跑,两者是完全不同的东西 —— 界面上也写了这句话。

**Base URL 里粘了整条端点也没关系。** 从文档的 curl 示例里复制
`https://apihub.agnes-ai.com/v1/images/generations` 填进 Base URL 框,
工具会把 `/images/generations` 剥掉再按模态重新拼 —— 这是真实踩过的坑:
端点写死成 `/chat/completions` 的年代,这条 URL 会被拼成
`.../v1/images/generations/chat/completions`,然后 404,而错误提示还在说
「请检查模型名是否正确」,把人往完全错误的方向带。

## 测试

```bash
# macOS / Linux
.venv/bin/python -m pytest tests/ -q

# Windows
.venv\Scripts\python.exe -m pytest tests/ -q
```

99 个测试,覆盖调度精度、重试规则、熔断逻辑、账目配平、六种模态的端点与
请求体、源素材解析(URL / Data URI / 本地文件)、计划量、原始报错回传、
实时数字刷新规则,以及端到端 HTTP 链路。

分发包里也可以直接自检(会自动补装 pytest):

```
Windows:  启动.bat --selftest
macOS:    python3 launcher.py --selftest
```

## 项目结构

```
rpm_verifier/
├── core/                  # 纯 Python,不依赖 Qt,可独立单测
│   ├── scheduler.py       # 绝对时间锚点调度器
│   ├── client.py          # httpx 连接池 + 503 重试 + 请求体预序列化
│   ├── modality.py        # 六种模态的端点 / 请求体 / 超时 / 计划量
│   ├── source.py          # 源素材解析:URL / 本地文件 → Data URI / 脱敏摘要
│   └── stats.py           # 计数器 + RPM 折算
├── ui/
│   ├── styles.py          # 设计令牌 + QSS
│   └── main_window.py     # PySide6 界面
└── app.py                 # 入口
assets/fonts/              # 可选。放入 .ttf/.otf 即自动启用
assets/chevron-down.svg    # 下拉框的自绘箭头(12×8,细线 chevron)
tests/
├── test_core.py           # 单元测试(测试替身)
├── test_input_modalities.py # 新模态请求体 / 源素材 / 计划量
├── test_ui.py             # 界面行为测试(offscreen,无需显示器)
└── test_integration.py    # 端到端测试(本地 mock HTTP 服务器)
```

**硬性约束:`core/` 中禁止 import 任何 Qt 模块。** 调度器是产品的核心资产,必须能被纯 pytest 验证。

## 界面层须知

`ui/styles.py` 是唯一存放色值、字号、间距的地方,改视觉只动这一个文件。

几个踩过的坑,改界面之前先看:

1. **必须 `app.setStyle("Fusion")`。** macOS 原生样式会自己绘制 `QLineEdit` /
   `QPushButton` 的底色,把 QSS 里的 `background` 吃掉,表现为「输入框和主按钮
   完全不生效」。
2. **不要用 `widget.setStyleSheet()` 给容器上底色。** Qt 的 widget 级样式表会
   向下级联到整棵子树,一条 `background: #f5f5f5` 能把所有卡片和按钮冲成灰色。
   回到 `styles.py` 用 ID 选择器写。
3. **QSS 没有 `box-shadow` 和 `letter-spacing`。** 卡片层次靠 1px 边框 + 画布/
   纸张的色调差建立;字距用 `styles.tighten()` 落到 `QFont` 上。
4. **`QComboBox` 不要并进 `QLineEdit` / `QSpinBox` 的同一条规则。** 那条规则把
   `selection-background-color` 设成 `INK`,下拉弹出列表会继承成一条刺眼的
   黑色选中条。`QComboBox` 单独写,选中色用 `CANVAS`;弹出列表是独立的
   `QComboBoxPrivateContainer` + `QComboBoxListView`,应用级 QSS 不一定命中,
   所以 `_style_combo_popup()` 直接在 view 上设 palette + 局部样式。
5. **参数是表单行,不是子表单。** 每个参数占一行:左边字段名、右边下拉框,
   和 Base URL / 模型名同一条标签列。不要再套「小标题 + 下拉」的缩进结构,
   那会让参数块和整张表单格格不入。

字体走**跨平台**系统栈:macOS 用 SF Pro Text / Helvetica Neue,
Windows 用 Segoe UI / Microsoft YaHei,各自取各自平台上的。想用 Geist 或
Inter,把字体文件丢进 `assets/fonts/` 即可,`load_bundled_fonts()` 会自动加载。

## 几条不能改的设计规则

写代码前请先理解这几条,它们都是需求澄清时明确敲定的:

1. **429 绝不重试。** 429 是本工具要*观测的对象*,不是要绕过的障碍。重试它会让主统计的 RPM 虚高,用户就再也找不到真实上限了。
2. **重试请求计入「全部请求」,但要和首次请求分列。** 503 重试同样打到了 API、消耗了额度,不能从状态码表和 RPM 里消失。表里用「首次 / 重试 / 合计」三列拆开:合计是真实流量,首次是限流诊断信号。
3. **调度必须用绝对时间锚点**(`t0 + n × interval`),不能写成 `await asyncio.sleep(interval)`,后者会累积漂移、系统性偏低。
4. **报告里不出现「达标 / 未达标」。** 只呈现数据,判断权交给用户。
5. **重试的硬边界是测试窗口。** 窗口一结束就不再重试,没成功的记为失败。
6. **发出去的每一个请求都必须有归宿。** 状态码表的总数必须和「已发送」相等。
   收尾等不及的请求要记成「未返回」,不许悄悄消失。观测工具的数,自己得先
   对得上账 —— 一旦出现「发了 20 个只有 19 个结果」,用户就没法再信任任何
   一个数字了。
7. **收尾等待由模态决定,而且必须有上限。** 文本 8 秒,图像 20 秒(图像是
   同步生成,单请求就要十几秒,给太短会白白丢掉最后一批状态码)。但绝不能
   让它跟着请求超时走 —— 图像超时 120 秒,收尾也等 120 秒的话,界面看上去
   就跟卡死了。等不及的请求记「未返回」,第 6 条依然成立。
8. **实时速率只在成功数变化时刷新。** 快照是 4Hz 推的,分子(成功数)要等
   响应才动,分母(已耗时)却一直在涨。每次快照都重算的话,数字会在两个响应
   之间一路下滑、响应一到又弹回去 —— 看上去就是无规律乱跳。
9. **没有拿到响应的请求,绝不能和 5xx 混为一谈。** 503 / 504 是服务器
   「回了话」;而超时、连接错误这些是压根没收到任何东西。状态码表里用
   负数常量表示后者,界面上有脚注说明。
10. **端点、请求体、超时都由模态决定,全部收在 `core/modality.py`。**
    散落在 client 和界面里的话,加一种模态就要改一圈,而且必然漏掉某处。
11. **探针只拦「配置错了」,不拦「服务器不痛快」。** 400/401/403/404/413/415/422
    才中止测试。429 和 5xx 一律放行 —— 它们恰恰证明地址和 key 都是对的,
    而 429 正是本工具要观测的对象,在门口就把它挡掉,用户永远测不到限流。
12. **「成功」是 2xx,不是 200。** 异步接口创建任务返回 201 / 202 都有可能,
    只认 200 的话成功数会一直是 0,用户会以为请求全挂了 —— 而服务器其实
    收下了。
13. **服务器说的话,原样带回来。** 非 2xx 时要把响应体里的错误说明抠出来,
    和状态码、实际请求地址一起摆给用户看。只报一句「服务器拒绝了请求参数」
    等于什么都没说:用户知道被拒了,却不知道为什么,只能一个个参数去试。
    探针失败时显示在弹窗里,正式测试里跟着报告出来 —— 两条路径都不能漏,
    因为「探针侥幸过了、正式跑起来才整片 400」一样会发生。
14. **源素材是固定输入,一次测试只用一个。** 不随机换图、不轮换素材;
    否则 RPM 的变化就分不清是限流还是素材差异。
15. **图生视频/视频生视频只接受公开 URL。** 服务端要自己拉素材,本地文件
    没有意义。只有图生图支持本地文件,因为它可以编码成 Data URI。
    这条差异必须收在 `core/modality.py` 的 `accepts_local_file` 里,不要
    在界面里写 `if 模态 == ...`。
16. **异步视频的「成功」是任务创建成功,不是视频生成成功。** 界面标签写
    「成功(任务创建)」,报告里不许声称视频已生成。生成在服务端后台跑,
    本工具不轮询。
17. **视频的 503 重试会重新创建任务,可能产生额外计费。** 这是明确确认过的
    取舍(所有模态保留 10 次重试);视频类模态的说明里必须写这句话。
18. **图生图的 Data URI 只读一次、只编码一次、请求体只序列化一次。**
    5 MB 的图 base64 后约 6.7 MB,每个请求都重读/重编码的话,高 RPM 下
    工具自己先成为瓶颈。
19. **源素材不落盘。** 签名 URL 等同凭据,不写进配置文件;报告只显示
    脱敏摘要,输入框保留用户输入的完整原文供编辑。
20. **「连接错误」必须带原始异常,不能只给一个 -2。** 没有 HTTP 响应不等于
    没有信息:`ConnectError`、`ConnectTimeout`、`SSLError`、
    `RemoteProtocolError` 指向完全不同的排查方向。状态码表里负数行显示成
    `连接错误(-2)`,报告下方「原始错误」直接列出 httpx 的异常原文;HTTP
    错误则列出原始状态码和服务器原话。

## 状态码表里的非 HTTP 行

状态码表按「首次 / 重试 / 合计 / 占比」展示。**合计 = 首次 + 重试,是本次
实际打到 API 的全部请求**;重试请求不被排除,也计入「全部请求 RPM」。
「成功 RPM(含重试)」按全部 2xx 折算,旁边同时给出首次请求 RPM,方便区分
「首次就成功」和「靠重试才成功」。

负数常量**不是** HTTP 状态码。区分它们是有意义的:把「连不上」记成 5xx,
用户就会以为被限流了。

| 值 | 显示 | 含义 | 该怪谁 |
|---|---|---|---|
| -1 | 响应超时 | 连上了,但服务器没在超时时间内回话 | 可能是 API 过载(限流信号) |
| -2 | 连接错误 | TCP/TLS/代理层就没通,或长连接被对端重置 | 网络 / Base URL / 代理 |
| -3 | 未返回 | 窗口结束收尾时仍未回来,被取消 | 接口响应太慢 |
| -4 | 连接超时 | 连 TCP 都超时了 | 网络 / Base URL |
| -5 | 连接池耗尽 | 本机并发连接不够用 | **我们自己**,与 API 无关 |

**连接错误没有 HTTP 状态码。** 这类请求在收到任何 HTTP 响应之前就失败了,
所以不可能给你一个「原始 HTTP 码」—— 硬编一个反而是假数据。工具能做的是
把原始异常原样带出来:状态码表里显示 `连接错误(-2)`,表格下方「原始错误」
直接列出 `ConnectError: ...`、`RemoteProtocolError: ...` 或 `SSLError: ...`。
HTTP 错误则显示 `HTTP 400 · 服务器原话:` 加上响应体里的说明。

⚠️ 判断异常类型时顺序不能反:`PoolTimeout` 和 `ConnectTimeout` 都是
`TimeoutException` 的子类,先判父类会把它们吞成同一个「响应超时」,导致
把本机的问题栽给 API。见 `client.classify_error()`,有测试守着。

## 已知限制

- 只支持 OpenAI 兼容协议(`/chat/completions`、`/images/generations`、
  `/videos`),不支持 Anthropic / Gemini 原生协议
- 只发非流式请求
- 视频/图生视频/视频生视频只测创建任务这一步,不轮询任务结果 —— 轮询测的是
  另一件事,而且会让 RPM 的含义变得含混;任务创建成功但后续生成失败,
  本工具看不到
- 图生视频/视频生视频只接受可公开访问的素材 URL;工具不提供上传、托管或
  公网隧道。图生图可以用本地图片,因为它能编码成 Data URI
- 图生图按 `extra_body.image` 构造请求体(Agnes 官方 raw curl 的写法);
  如果你的网关要求顶层 `image`,需要单独适配
- 一次测试只用一个源素材;不支持多图合成、音频参考、多视频参考
- 不做历史记录、导出、长期监控;源素材不落盘

## 分发打包

发给同事的是源码包 + 一键启动器,不是 PyInstaller 打的单文件。理由是
源码分发**天然绕开**了 PyInstaller 那一堆坑:没有 `sys._MEIPASS` 路径重写、
没有 Gatekeeper 签名、没有 arm64 / Intel 架构问题,`styles.py` 里
`Path(__file__).resolve().parents[2] / "assets" / "fonts"` 这种相对路径也算得对,
因为目录结构原样保留。

```
launcher.py                     # 启动器:查版本 → 建 venv → 装依赖 → 起界面
启动.bat / 启动.command          # 两个平台的双击入口,只负责找到 Python
packaging/build_release.py      # 构建分发包
```

构建:

```bash
.venv/bin/python packaging/build_release.py
```

产物在 `dist/`,**只写 dist/,不碰源码目录**。

几条关键的取舍:

1. **依赖锁 `==` 精确版本,不用 `>=`。** 版本是构建时从开发机实际环境里读出来的。
   同事装到的必须和我们验证过的完全一致,否则「在我这儿好好的」就是句空话。
2. **依赖写 `PySide6-Essentials`,不写 `PySide6`。** 元包会把 QtWebEngine 之类的
   Addons 一起拖下来,光 `QtWebEngineCore` 一个就 589MB,`.venv` 因此涨到 1.2G;
   而本程序只用到 QtCore / QtGui / QtWidgets,全在 Essentials 里。实测差三倍多。
3. **启动器用 Python 写,不写 shell / bat。** 两个平台共用一份;
   venv 解释器路径两边不同(`Scripts\python.exe` vs `bin/python`);
   而且中文 Windows 控制台默认 GBK,shell 里 `echo` 中文会直接
   `UnicodeEncodeError`。
4. **版本区间两头都要拦。** 只拦下限的话,同事装了 3.15 会撞进 pip 的依赖解析
   报错里 —— 那堆输出没人看得懂,更看不出是版本问题。

图标由 `scripts/make_icon.py` 生成到 `assets/`,一次出三种格式
(`icon.png` / `icon.ico` / `icon.icns`),三个平台都能重画。
ICO 是手写二进制打包的多尺寸文件 —— Qt 的 ICO writer 只写单个尺寸,
而且是未压缩 BGRA(一张 256×256 就 270KB);自己打包可以塞进
16/24/32/48/64/128/256 七个尺寸、每张用 PNG 压缩,总共才 22KB。
