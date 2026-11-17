[English](synthetic-recovery.md) | 简体中文

# 用合成数据跑一遍恢复流程

这个练习会在临时目录生成一段虚构的导入历史，不读取真实 Claude 或 Codex 数据，也不需要登录。请在包含本版本代码的检出目录中执行，并先安装 `session-doctor`。直接运行源码时，把下面函数里的 `session-doctor` 换成 `./session-doctor`。

```sh
DEMO=$(mktemp -d)
DEMO=$(cd "$DEMO" && pwd -P)
THREAD_ID=01900000-0000-7000-8000-000000000123

XDG_STATE_HOME="$DEMO/bootstrap" SESSION_DOCTOR_NATIVE_TEST_BIN= python3 -B - "$DEMO" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tests"))
from test_checkpoint_recovery import create_recovery_fixture
from test_session_doctor import Fixture

create_recovery_fixture(Fixture(Path(sys.argv[1])))
PY

doctor() {
  session-doctor --claude-home "$DEMO/claude" --codex-home "$DEMO/codex" \
    --state-dir "$DEMO/private state" "$@"
}

doctor list
doctor report "$THREAD_ID"
doctor recover "$THREAD_ID"
```

预期看到 `SOURCE_CHECKPOINT_LOST` 和 `READY_FOR_ISOLATED_CANDIDATE`。示例账本只关联合成源文件。这三步无需验证器或 Codex 账号。

接下来构建验证器和候选。构建验证器可能下载固定的公开源码与 Cargo 依赖，整个过程不调用模型。如已有可信的本地验证器，可参考[原生验证](../README.zh-CN.md#原生验证)和 `session-doctor validator-build --help`，也可以通过 `SESSION_DOCTOR_NATIVE_TEST_BIN` 显式指定现有验证器来构建候选。

```sh
doctor validator-build
doctor recover "$THREAD_ID" --build
```

示例会重建 7 个合成模型条目，成功时输出 `CANDIDATE_READY`。原合成 rollout 会完整保留，新的启动用量估算也会被核对。已构建过时可能返回 `CANDIDATE_ALREADY_BUILT`。

最后两步仅供对照。合成例子没有真实的 Desktop 会话或 Codex 账号，因此保留注释，不执行。

```sh
# doctor recover "$THREAD_ID" --register
# doctor recover "$THREAD_ID" --test
```

| 命令 | 只读 | 写入哪些本地文件 | 修改 Codex 状态 | 模型额度 | 如何确认成功 |
| --- | --- | --- | --- | --- | --- |
| `list` | 是 | 无 | 无 | 否 | 列出虚构原会话。 |
| `report THREAD_ID` | 是 | 无 | 无 | 否 | 输出 `SOURCE_CHECKPOINT_LOST`。 |
| `recover THREAD_ID` | 是 | 无 | 无 | 否 | 输出 `RECOVERY_PREVIEW`，计划可生成候选。 |
| `validator-build` | 否 | 私有验证器、构建文件和缓存 | 无 | 否 | 输出 `VALIDATOR_READY`。 |
| `recover THREAD_ID --build` | 否 | 隔离候选和操作日志 | 无 | 否 | 原生验证通过并输出 `CANDIDATE_READY` 或 `CANDIDATE_ALREADY_BUILT`。 |
| `recover THREAD_ID --register` | 否 | 操作日志和新会话文件 | 是，创建新会话 | 否 | 输出 `REGISTERED` 或 `ALREADY_REGISTERED`，附新 ID。 |
| `recover THREAD_ID --test` | 否 | 操作日志和新会话的测试轮 | 是，增加一轮对话 | 是，可能消耗额度 | 输出 `CONTINUATION_PASSED`，并给出新会话中的验证证据。 |

本示例所有 ID、输入、路径和示例输出均为虚构或临时生成。真实恢复的前置条件和中断处理见[中文主文档](../README.zh-CN.md)。
