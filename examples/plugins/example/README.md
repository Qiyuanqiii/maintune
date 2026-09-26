# Workflow Notes · Maintune Plugin API v2 示例

这是一个可安装的第三方插件示例，展示如何只使用公开的 `maintune-plugin-sdk` 开发插件。它在专属 `data_dir` 保存少量任务生命周期记录，提供读取统计的 Service 和 Agent Tool；不导入 Maintune Core 私有模块。当前为 Preview 示例，不代表完整 Provider 实现。

## 包与运行

从本目录运行 `python build_mtp.py`，得到 `dist/workflow-notes-example.mtp`。在支持 Plugin API v2 的 Maintune 中安装并启用。包使用 `plugin_api: 2`、独立进程 `isolated` 和标准 `requirements.txt`；本例没有额外的 pip 依赖。`manifest.yaml` 采用 JSON 写法，可由 Maintune 的安全 YAML 子集解析器读取。

| 扩展 | 名称 | 状态 | 行为 |
| --- | --- | --- | --- |
| Hook | `task.started` | Stable | 根据 `record_started` 配置保存一条启动观察记录 |
| Finalizer | `task.finally` | Stable | Task 结束时记录状态；用 `invocation_id` 防止重试重复写入 |
| Hook | `pr.review` | Experimental | 返回 `continue`，不改写审查决策 |
| Service | `example.workflow-notes/activity.stats` | v2 | 返回已记录的启动和结束计数 |
| Agent Tool | `example.workflow-notes/activity_count` | v2 | 按 `started` 或 `finalized` 查询计数；推荐 `code_worker`，安装后仍需管理员启用 |

配置字段：`label`（Tool 输出标签，默认 `Maintune`）、`record_started`（是否记录启动事件，默认 `true`）。数据写入宿主给出的 `data_dir/events`。每次调用以 `kind + invocation_id` 生成固定文件名；记录只保留 Task ID、最终状态和 attempt，不保存 Hook 全文、Prompt 或凭据。

`manifest.yaml` 还展示一个 **可选** Maintune 插件依赖 `example.anysearch`。未安装它不影响本例运行；本例也不会调用其 Tool。它用于说明 optional dependency 的声明方式。

`ui/index.html` 是可选的、只读的插件专属页面，没有脚本，也不注入 Maintune Core 页面。它演示静态 UI 资源打包，不承诺当前 Preview 宿主一定已向浏览器开放该资源。

`src/provider_example.py` 演示 `register_model_provider` 和带 `secret: true` 的 Provider 配置 Schema。此文件**没有被 `register(api)` 调用**：其中的处理函数明确抛出 `NotImplementedError`，开发者必须实现真正的模型传输与返回数据后才能注册。不要把它当作可用 Model Provider。

## 测试

安装公开 SDK 后，在本目录运行：

```sh
python -m unittest discover -s tests -v
```

在 Maintune 源码工作区可设置 `PYTHONPATH` 为根目录下的 `sdk`。测试覆盖注册、Hook、finalizer 幂等性、Service、Tool、配置、Provider 注册示意和可复现 `.mtp` 构建；无需网络、数据库或 Maintune Core。

本示例沿用 Maintune 源代码的 AGPL-3.0-only；独立 SDK 遵循其 MIT 许可。

---

## English

Workflow Notes is a runnable third-party **Plugin API v2** example. It imports only the public `maintune-plugin-sdk`, records a small task lifecycle DTO under its own `data_dir`, and exposes a Service and an Agent Tool. It does not import Maintune Core internals. This is a Preview example, not a complete Provider implementation.

Run `python build_mtp.py` to create `dist/workflow-notes-example.mtp`, then install and enable it in a Maintune build with Plugin API v2. The package uses the isolated runtime and a standard empty `requirements.txt`. Its JSON-formatted `manifest.yaml` is accepted by Maintune's safe YAML-subset parser.

The Stable `task.started` Hook saves an observation when `record_started` is true. The Stable `task.finally` finalizer records Task status and attempt. Records use a filename derived from `kind + invocation_id`, so replaying the same invocation does not duplicate a record. The Experimental `pr.review` Hook always returns `continue`; it never changes the review decision. The namespaced Service `example.workflow-notes/activity.stats` returns start/final counts. The namespaced Tool `example.workflow-notes/activity_count` reads one count and recommends `code_worker`; an administrator must still enable it. Preview 3 injects plugin Tools only into `code_worker`.

Configuration contains `label` (default `Maintune`) and `record_started` (default `true`). Stored records contain only Task ID, final status and attempt, never the complete Hook payload or credentials. The manifest's `example.anysearch` dependency is **optional** and demonstrates dependency metadata only; this plugin never calls that Tool.

`ui/index.html` is a static, read-only optional plugin page with no JavaScript or Core page injection. It demonstrates packaging a UI asset and does not claim that every Preview host already serves it. `src/provider_example.py` shows `register_model_provider` with a secret config field but is **not registered by default**. Its handler intentionally raises `NotImplementedError`; implement a real model transport and response contract before registering it.

After installing the public SDK, run `python -m unittest discover -s tests -v` here. From the Maintune source tree, set `PYTHONPATH` to the root `sdk` directory. Tests need no network, database or Maintune Core. This example follows the Maintune source AGPL-3.0-only license; the separate SDK is MIT licensed.
