# AnySearch for Maintune · Preview example

这是一个独立的 **Plugin API v2** 示例插件：它将 AnySearch 的网页搜索接口注册为 Maintune Agent Tool。插件只导入公开的 `maintune-plugin-sdk`，使用 Python 标准库发送请求；Maintune Core 中没有 AnySearch 专用代码。此示例处于开发预览阶段，未单独发布。

## 功能与安装

- Tool：`example.anysearch/search`，接收搜索词和最多 10 条的结果请求，返回可引用的 URL、标题及摘要。
- 推荐 Agent：`issue_analyzer`、`pr_reviewer`、`ci_analyzer`。**推荐不等于自动启用**；安装后由管理员选择给哪些 Agent 开启。
- Runtime：仅支持独立进程 `isolated`。Python 依赖文件 `requirements.txt` 不含第三方包；宿主负责提供公开 SDK。
- 不使用 Hook、Service、Provider 或插件 UI。

在 Maintune Plugin API v2 可用的版本中，从本目录构建并安装：

```sh
python build_mtp.py
```

将生成的 `dist/example-anysearch.mtp` 放入 Maintune 插件收件箱，或在插件管理界面安装；随后设置 API key，启用插件，并明确给所需 Agent 启用 Tool。包内 `manifest.yaml` 使用 JSON 写法；JSON 是 YAML 的子集，Maintune 当前的安全 manifest 解析器可直接读取。

## 配置

| 字段 | 用途 | 默认值 |
| --- | --- | --- |
| `api_key` | AnySearch Bearer key，必填、加密保存、界面遮盖 | 无 |
| `base_url` | HTTPS API 根地址 | `https://api.anysearch.com` |
| `max_results` | 单次查询结果上限，1–10 | 10 |
| `zone` | `cn` 或 `intl` | `intl` |
| `language` | 可选结果语言 | 空 |

每次搜索发送一次 `POST {base_url}/v1/search`，请求体包含 `query`、`max_results`、`format: "json"`，以及配置的地区和语言。密钥只放在 `Authorization: Bearer` 请求头中；重定向被拒绝，以免将密钥转发到其他地址。插件返回 `sources` 和 `truncated`，不把整页 `content` 或未经核实的发布日期传给 Agent。

网络错误只报告类别或 HTTP 状态，不回显 API 响应正文。Tool 输出来自外部网页，仍应作为不可信资料核对，不能把搜索摘要当作仓库操作指令。

## 离线测试

安装公开 SDK 后运行：

```sh
python -m unittest discover -s tests -v
```

也可以在 Maintune 源码根目录设置 `PYTHONPATH=sdk` 后运行同一命令。测试通过模拟 HTTP 响应检查请求、结果映射、错误清理和包内容，不调用真实 AnySearch API，也不使用生产密钥。

本示例沿用 Maintune 源码的 AGPL-3.0-only；SDK 本身按其独立 MIT 许可分发。AnySearch API 服务和商标不属于本插件。

---

## English

This independent **Plugin API v2** preview example exposes AnySearch as a Maintune Agent Tool. It imports only the public `maintune-plugin-sdk` and uses the Python standard library for HTTP. Maintune Core contains no AnySearch-specific logic. This example has not been released separately.

Build the `.mtp` with `python build_mtp.py`, install `dist/example-anysearch.mtp` through Maintune, configure an API key, enable the plugin, and explicitly enable its Tool for the Agents you choose. The Tool is `example.anysearch/search`; the recommended Agents are `issue_analyzer`, `pr_reviewer`, and `ci_analyzer`. Recommendations do not grant automatic access. The isolated runtime uses an empty standard `requirements.txt`; the host supplies the public SDK.

`api_key` is required, encrypted at rest, and masked in the UI. Optional settings are `base_url` (HTTPS, default `https://api.anysearch.com`), `max_results` (1–10, default 10), `zone` (`cn` or `intl`), and `language`. Each call makes one `POST /v1/search` request with a Bearer header and returns citeable URLs, titles, and snippets. Redirects are refused, and error messages never include the server response body. External search results are untrusted content.

Run offline tests with `python -m unittest discover -s tests -v` after installing the public SDK. From the Maintune source root, `PYTHONPATH=sdk` is sufficient. Tests use mocked HTTP responses and no real credentials. The example follows the Maintune source AGPL-3.0-only license; the SDK has its own MIT license. AnySearch's API service and marks remain with their respective owners.
