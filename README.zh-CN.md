[English](README.md) | 简体中文

# Codex Session Doctor

Session Doctor 用于诊断从 Claude Code 导入 Codex 的会话。确认有可靠的恢复依据后，它还能用原历史中的压缩摘要重建模型上下文，再注册为一个新的 Codex 会话。这是独立社区项目，与 OpenAI、Anthropic 无隶属关系。

## 为什么导入后会超限

Claude Code 会在对话变长时做 **compaction（上下文压缩）**，用摘要、明确保留的消息和后续内容代替较早的工作上下文。**checkpoint（检查点）** 标记了这次替换。已审查的 Codex 导入器会丢掉 Claude 的压缩边界，并可能把摘要当成一条普通用户消息。旧内容因此仍参与模型历史重建，后续请求可能超出上下文窗口，也可能超出 API 的输入条目数上限。

Session Doctor 先核对本地导入账本和源历史，再根据已验证的 Claude 检查点构造隔离候选。浏览时仍可查看完整的导入记录。模型回放从检查点的替换历史开始，随后接上剩余记录。较早的完整历史保留在磁盘上，无须重新进入模型上下文。[上游根因和合成复现](https://github.com/EZ3r/codex-session-doctor/tree/main/upstream)

## 能做什么

- 找到能从账本追溯来源的导入会话，生成脱敏的只读诊断。
- 恢复可核验的摘要、原检查点保留的内容和逻辑后续，保住导入后新增的 Codex 工作。
- 通过本机编译的 Codex 原生验证器，核对历史重建和启动用量基线。
- 显式注册一个新的本地会话，并按需发送一次真实续聊测试。
- 注册或续聊请求意外中断时，按本地已持久化的证据核对结果。

## 环境要求和兼容性

| 用途 | 要求 |
| --- | --- |
| npm CLI | macOS 或 Linux、Node.js 22+、npm、Python 3.11+ |
| Python 或源码方式 | macOS 或 Linux、Python 3.11+ |
| 编译原生验证器 | Git、通过 rustup 管理的 Rust。具体工具链由固定版本的 Codex 源码决定。 |
| 注册或续聊测试 | PATH 中可运行的 Codex CLI `0.169.0-alpha.11.1`、已登录的 OpenAI 账号，以及通过原生验证的候选 |

目前不支持 Windows。自动恢复只适用于来源唯一、源文件仍与账本匹配的受支持 **Legacy history** 导入。工具可以诊断 Paginated 模式的回放条件，自动恢复暂不涵盖该模式。版本未知、来源歧义、数据已变化、格式不安全或新会话身份无法确认时，相关操作会被拦下。支持其他 Codex 版本需要重新审查写入、解码、回放与 App Server 协议，并重建验证器。

## 安装

### npm（首选）

**`codex-session-doctor` 发布到 npm 之后**，使用下面的命令。

```sh
npm install -g codex-session-doctor
session-doctor --version
session-doctor --help
```

发布之前，可以进入含有 `package.json` 的项目检出目录，从本地 tarball 安装。

```sh
npm pack
npm install -g ./codex-session-doctor-0.2.0.tgz
session-doctor --version
```

npm 包内含与源码相同的 Python 核心和 Rust 测试源码。npm 包与 Python 发行包共用同一个版本号。安装只写入包文件并链接命令，没有生命周期脚本。第一次运行时，启动器会检查 PATH 中是否有 Python 3.11+。如果 Python 装在其他位置，可把 `SESSION_DOCTOR_PYTHON` 指向可信的解释器。启动器直接执行该文件，支持含中文、空格和特殊字符的路径。

```sh
SESSION_DOCTOR_PYTHON="$(command -v python3.13)" session-doctor --version
```

### pipx

安装好 [pipx](https://pipx.pypa.io/) 后，从项目源码目录安装。请使用 Python 3.11+。

```sh
git clone https://github.com/EZ3r/codex-session-doctor.git
cd codex-session-doctor
pipx install .
session-doctor --version
```

也可以在 Python 虚拟环境中运行 `python -m pip install .`。

### 直接运行源码

进入源码检出目录后可继续使用原有 Python 入口，把下文中的 `session-doctor` 换成 `./session-doctor`。

```sh
./session-doctor --help
./session-doctor list
```

## 快速诊断

从 `list` 或 Codex Desktop 会话链接取得**原始导入 Thread ID**。最后两条命令中的变量请换成自己的 ID，下面用的是虚构值。

```sh
session-doctor list
THREAD_ID=00000000-0000-4000-8000-000000000101
session-doctor report "$THREAD_ID"
session-doctor recover "$THREAD_ID"
```

`report` 会省略直接标识和对话正文。单独运行 `recover` 只核对映射、计算恢复计划并列出支持的运行时版本。它不启动 Codex、不执行原生验证器、不创建文件，也不发起模型请求。练习操作时可先跑[离线合成示例](docs/synthetic-recovery.zh-CN.md)。

## 从诊断到恢复

### 1. 构建候选并做原生验证

```sh
session-doctor validator-build
session-doctor recover "$THREAD_ID" --build
```

`validator-build` 显式构建本地原生验证器。第一次可能下载公开源码和 Rust 依赖，耗时数分钟并占用数 GB。`recover --build` 生成私有候选，检查历史重建和启动用量基线。成功时显示 `CANDIDATE_READY` 或 `CANDIDATE_ALREADY_BUILT`。如需再验一次，把下面的虚构路径换成输出的 `candidate_path`。

```sh
CANDIDATE_PATH=/fictional/recovery/candidate-example
session-doctor validate-candidate "$CANDIDATE_PATH"
```

### 2. 注册新会话

```sh
codex --version
session-doctor recover "$THREAD_ID" --register
```

这个步骤通过 Codex App Server 的实验性 fork-from-path 功能注册。Codex 分配新 ID，标题使用原名加 ` (re)`，没有原名时用本地预览。它会创建一个新的 Codex 会话并更新 Session Doctor 日志，保留原会话、Claude 源文件和导入账本。注册不发送模型消息。看到 `REGISTERED` 或 `ALREADY_REGISTERED` 后，复制返回的**新** `thread_id`。macOS 可用下面的命令打开，其他有 Codex 链接处理器的安装也可打开对应链接。

```sh
NEW_THREAD_ID=00000000-0000-4000-8000-000000000102
open "codex://threads/$NEW_THREAD_ID"
```

### 3. 按需验证真实续聊

```sh
session-doctor recover "$THREAD_ID" --test
```

这个选项单独授权发送一次真实续聊。恢复后的历史会作为正常的模型上下文提交，请求可能消耗模型额度。`CONTINUATION_PASSED` 表示工具已核实当轮完成，并给出是否触发压缩等落盘证据。命令仍接收**原始** ID，再从操作日志定位新会话。

## 恢复如何工作

- **compaction** 缩减模型的工作历史。**checkpoint** 记录替换发生的位置，以及从哪里、以什么状态恢复。
- **Legacy history** 是当前自动恢复支持的 Codex rollout 模式。用来恢复的检查点要满足固定版本对历史字段和恢复元数据的要求。
- **`replacement_history`（替换历史）** 是检查点之后模型历史的基底。工具用核实过的 Claude 摘要、保留内容和逻辑后续构造它，再由 Codex 回放剩余记录。
- 原生验证覆盖解码、模型历史重建和启动估算。离线 Token 只是近似值，也不含实时请求的全部开销。服务端是否接受，只能用真实续聊确认。

## 原生验证

npm 包内含 Rust 测试源码，运行 `validator-build` 时才针对[固定的 Codex 提交](https://github.com/openai/codex/commit/0184dfa6a48f0110dd9c6835522492908e5abcf3)本机编译。也可以指定包含该提交的本地检出目录。只有 Cargo 依赖已缓存时，才使用 `--offline`。

```sh
CODEX_SOURCE=/fictional/src/codex
session-doctor validator-build --codex-source "$CODEX_SOURCE" --offline
```

npm、Python 安装和直接运行源码使用相同的验证门槛。`NATIVE_VALIDATED` 说明原生验证已运行并通过。`NATIVE_VALIDATION_REQUIRED` 表示目前只核对了候选的文件完整性。高级命令的预览可能执行已有的原生验证器，只应指定自己构建且信任的二进制文件。

## 安全与隐私

- npm 安装不会运行 CLI、读取会话文件、安装 Python、Rust、Git 或 Codex，也不会发起模型请求。运行时请使用可信的解释器、PATH 和状态目录。
- `list`、`report` 和不加阶段参数的 `recover` 只读。`validator-build` 和 `recover --build` 只写本地验证器或候选状态。`--register` 会创建独立的 Codex 新会话。只有显式测试命令才发送真实模型请求。
- 高级命令遵循同样的边界。`register-candidate --apply` 创建会话，`test-continuation --send` 发送真实测试轮。独立构建、验证和重设启动基线等命令见 `session-doctor --help`。
- 候选可能含完整对话、工具输出和元数据，应保持私有。真实会话的脱敏报告也应留在本机，请勿把候选和报告提交到 Git 或公开上传。
- 同一个 Codex home 应始终使用同一个私有状态目录。默认位置是 `${XDG_STATE_HOME:-$HOME/.local/state}/session-doctor`。可用 `CODEX_HOME`、`CLAUDE_CONFIG_DIR` 指定输入目录。`--state-dir`、`--codex-home` 和 `--claude-home` 等全局参数放在子命令前。
- 注册和续聊请求发送前会先写入持久化日志。中断后，用同一原始 ID 和状态目录重跑相同命令，工具会先对账。无法确认的操作继续保持待定。已经手动续聊或尝试过测试的会话不会再自动发送测试。

## 常见问题

| 现象 | 排查 |
| --- | --- |
| 找不到 `session-doctor` | 执行 `npm prefix -g`，检查该目录下的 `bin` 是否已加入 PATH。 |
| Python 找不到或版本太低 | 单独安装 Python 3.11+，或者把 `SESSION_DOCTOR_PYTHON` 指向已有解释器。 |
| 诊断为 `UNKNOWN` 或源文件哈希变化 | 查看 `report` 的证据代码。工具要求来源唯一，并且文件字节与导入账本匹配。请保留原始历史，拿到准确的导入快照后再恢复。 |
| 验证器缺失或候选未通过验证 | 运行 `validator-build`，核对 Git、Rust、固定提交和当前状态目录。验证错误解决后才能注册新会话。 |
| 不支持当前运行时 | 用 `codex --version` 核对上述支持版本。只读诊断仍可使用。 |
| 注册或续聊一直待定 | 保留候选和状态目录。以原始 ID 重跑同一项明确授权的操作，由日志核对已落盘的结果。 |

## 开发和测试

```sh
npm test
python3 -B -m unittest discover -s tests -v
npm pack --dry-run
npm pack
```

npm 测试会在临时前缀离线安装 tarball，使用合成历史验证。如果还要通过已安装的 npm 命令执行原生回放，把 `SESSION_DOCTOR_NATIVE_TEST_BIN` 指向可信的已构建验证器。Python 测试也会识别默认目录的验证器。打包和人工发布步骤见 [npm 发布检查清单](docs/npm-release.md)。

## 上游提案、贡献和许可证

面向 Codex 导入器的修复设计独立于 Session Doctor CLI。请查看[上游提案及 Rust 复现](https://github.com/EZ3r/codex-session-doctor/tree/main/upstream)和 [Codex Issue #34656](https://github.com/openai/codex/issues/34656)。

[贡献指南](CONTRIBUTING.md) · [安全政策](SECURITY.md) · [Apache-2.0 许可证](LICENSE) · [来源声明](NOTICE) · [依赖来源](docs/dependencies.md) · [更新记录](CHANGELOG.md)
