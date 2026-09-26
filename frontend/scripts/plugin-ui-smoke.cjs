const { chromium } = require("playwright-core");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const output = path.resolve(process.env.MAINTUNE_PLUGIN_UI_SMOKE_OUTPUT || path.join(root, "test-results", `plugin-ui-${process.pid}-${Date.now()}`));
const state = path.join(output, "state");
const pluginRoot = path.join(state, "plugins");
const anySearchPackagePath = path.join(root, "examples", "plugins", "anysearch", "dist", "example-anysearch.mtp");
const anySearchPackageName = path.basename(anySearchPackagePath);
const token = "plugin-ui-smoke-admin-token-0000000000001";
const bridgeToken = "plugin-ui-smoke-bridge-token-000000000000000000000001";
const port = 18778;
const python = process.env.PYTHON_PATH || path.join(root, ".venv", "Scripts", "python.exe");
const chrome = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const packagePath = process.env.MAINTUNE_ASTRBOT_PLUGIN_MTP || path.resolve(root, "..", "maintune-plugin-astrbot", "dist", "maintune-plugin-astrbot-0.1.0-dev.mtp");
const packageName = path.basename(packagePath);

fs.mkdirSync(output, { recursive: true });
fs.mkdirSync(path.join(pluginRoot, "inbox"), { recursive: true });
if (!fs.existsSync(packagePath)) throw new Error(`Plugin package missing: ${packagePath}`);
if (!fs.existsSync(anySearchPackagePath)) throw new Error(`AnySearch plugin package missing: ${anySearchPackagePath}`);
fs.copyFileSync(packagePath, path.join(pluginRoot, "inbox", packageName));
fs.copyFileSync(anySearchPackagePath, path.join(pluginRoot, "inbox", anySearchPackageName));

function waitFor(url, timeout = 30000) {
  const end = Date.now() + timeout;
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try { const response = await fetch(url); if (response.ok) return resolve(); } catch {}
      if (Date.now() >= end) return reject(new Error(`Timed out waiting for ${url}`));
      setTimeout(poll, 200);
    };
    poll();
  });
}

async function request(pathname, method = "GET", body) {
  const response = await fetch(`http://127.0.0.1:${port}/api${pathname}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error(`${method} ${pathname}: ${response.status} ${await response.text()}`);
  return response.status === 204 ? null : response.json();
}

(async () => {
  const backend = spawn(python, ["-m", "uvicorn", "maintainer.api:create_app", "--factory", "--host", "127.0.0.1", "--port", String(port)], {
    cwd: root,
    env: {
      ...process.env,
      MAINTAINER_ADMIN_TOKEN: token,
      MAINTAINER_ENCRYPTION_KEY: "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
      MAINTAINER_DATABASE_URL: `sqlite:///${path.join(state, "maintainer.db").replaceAll("\\", "/")}`,
      MAINTAINER_WORKSPACE_ROOT: path.join(state, "workspaces"),
      MAINTAINER_PLUGIN_ROOT: pluginRoot,
      MAINTAINER_STATIC_DIR: path.join(root, "frontend", "dist"),
      HTTP_PROXY: "http://127.0.0.1:9",
      HTTPS_PROXY: "http://127.0.0.1:9",
      ALL_PROXY: "http://127.0.0.1:9",
      http_proxy: "http://127.0.0.1:9",
      https_proxy: "http://127.0.0.1:9",
      all_proxy: "http://127.0.0.1:9",
      NO_PROXY: "127.0.0.1,localhost",
      no_proxy: "127.0.0.1,localhost",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  backend.stdout.on("data", chunk => log += chunk.toString());
  backend.stderr.on("data", chunk => log += chunk.toString());
  let browser;
  try {
    await waitFor(`http://127.0.0.1:${port}/healthz`);
    await request(`/plugins/install/${encodeURIComponent(packageName)}`, "POST");
    await request("/plugins/official.astrbot-bridge/config", "PUT", { bridge_token: bridgeToken, notify_events: ["task.waiting_for_owner", "task.failed"] });
    await request("/plugins/official.astrbot-bridge/enable", "POST");
    const readmePath = path.join(pluginRoot, "installed", "official.astrbot-bridge", "README.md");
    fs.appendFileSync(readmePath, "\n## Renderer safety probe\n\n<script>globalThis.__maintuneReadmeXss = true</script>\n\n[unsafe](javascript:globalThis.__maintuneReadmeXss=true)\n\n```js\nconsole.log('shown, not run')\n```\n");

    browser = await chromium.launch({ executablePath: chrome, headless: true, args: ["--no-proxy-server", "--disable-extensions", "--disable-background-networking"] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 950 }, serviceWorkers: "block" });
    await context.addInitScript(() => localStorage.setItem("maintainer.locale", "zh-CN"));
    const page = await context.newPage();
    const errors = [];
    const origin = `http://127.0.0.1:${port}`;
    await page.route("**/*", async route => {
      if (new URL(route.request().url()).origin !== origin) {
        errors.push(`external browser request blocked: ${route.request().url()}`);
        await route.abort();
        return;
      }
      await route.continue();
    });
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    page.on("pageerror", error => errors.push(error.message));
    page.on("requestfailed", request => errors.push(`request failed: ${request.url()}`));
    page.on("response", response => { if (response.status() >= 400) errors.push(`HTTP ${response.status()}: ${response.url()}`); });
    await page.goto(origin, { waitUntil: "networkidle" });
    await page.getByLabel("管理员访问令牌").fill(token);
    await page.getByRole("button", { name: "进入控制台 →" }).click();
    const wizard = page.getByRole("dialog", { name: "首次运行设置向导" });
    await wizard.waitFor();
    await page.locator(".wizard-steps button.text").click();
    await wizard.waitFor({ state: "hidden" });
    await page.locator("nav button").filter({ hasText: "插件与扩展" }).click();
    const card = page.locator(".plugin-card").filter({ hasText: "AstrBot Bridge" });
    await card.waitFor();
    if (await card.locator("input[type=password]").count()) throw new Error("Plugin config is expanded on the card");
    await page.screenshot({ path: path.join(output, "plugins-desktop.png"), fullPage: true });

    await card.getByRole("button", { name: "文档 / README" }).click();
    const readme = page.getByRole("dialog", { name: "AstrBot Bridge" });
    await readme.getByRole("heading", { name: "功能" }).waitFor();
    await readme.getByText("Renderer safety probe").waitFor();
    if (await page.evaluate(() => globalThis.__maintuneReadmeXss === true)) throw new Error("README script executed");
    if (await readme.locator('a[href^="javascript:"]').count()) throw new Error("Unsafe README link was rendered");
    if (await readme.locator("pre code").count() < 1) throw new Error("README code block was not rendered");
    await page.screenshot({ path: path.join(output, "plugin-readme.png"), fullPage: true });
    await readme.getByRole("button", { name: "关闭" }).click();

    await card.getByRole("button", { name: "设置" }).click();
    const settings = page.getByRole("dialog", { name: "插件设置" });
    const secret = settings.locator('input[type="password"]');
    await secret.waitFor();
    const secretValue = await secret.inputValue();
    if (!secretValue || secretValue === bridgeToken || !/^[*•]+$/.test(secretValue)) throw new Error("Plugin secret is not masked");
    await settings.getByText("事件订阅").waitFor();
    await page.screenshot({ path: path.join(output, "plugin-settings.png"), fullPage: true });
    await settings.getByRole("button", { name: "关闭" }).click();

    const toggle = card.getByRole("checkbox", { name: /启用状态/ });
    await toggle.click();
    await page.waitForFunction(() => document.querySelector(".plugin-card .badge.stopped"));
    await toggle.click();
    await page.waitForFunction(() => document.querySelector(".plugin-card .badge.running"));
    await card.getByRole("button", { name: "重新加载" }).click();
    await page.getByRole("status").filter({ hasText: "插件已重新加载" }).waitFor();

    await page.getByLabel("插件包文件名").fill(anySearchPackageName);
    await page.getByRole("button", { name: "安装插件" }).click();
    const anySearchCard = page.locator(".plugin-card").filter({ hasText: "AnySearch Web Search" });
    await anySearchCard.waitFor();
    if (!(await anySearchCard.getByText("API v2").count())) throw new Error("AnySearch was not identified as Plugin API v2");
    await anySearchCard.getByRole("button", { name: "设置" }).click();
    const anySearchSettings = page.getByRole("dialog", { name: "插件设置" });
    const anySearchSecret = anySearchSettings.locator('input[type="password"]');
    await anySearchSecret.waitFor();
    await anySearchSecret.fill("smoke-test-dummy-key-never-valid");
    await anySearchSettings.getByRole("button", { name: "保存" }).click();
    await page.getByRole("status").filter({ hasText: "已保存" }).waitFor();
    const anySearchToggle = anySearchCard.getByRole("checkbox", { name: /启用状态/ });
    await anySearchToggle.click();
    await page.waitForFunction(() => [...document.querySelectorAll(".plugin-card")].some(card => card.textContent.includes("AnySearch Web Search") && card.querySelector(".badge.running")));
    await anySearchCard.locator("summary").click();
    await anySearchCard.getByText("example.anysearch/search").waitFor();
    await anySearchCard.locator("summary").click();
    await anySearchToggle.click();
    await page.waitForFunction(() => [...document.querySelectorAll(".plugin-card")].some(card => card.textContent.includes("AnySearch Web Search") && card.querySelector(".badge.stopped")));
    await anySearchToggle.click();
    await page.waitForFunction(() => [...document.querySelectorAll(".plugin-card")].some(card => card.textContent.includes("AnySearch Web Search") && card.querySelector(".badge.running")));
    await anySearchCard.getByRole("button", { name: "重新加载" }).click();
    await page.getByRole("status").filter({ hasText: "插件已重新加载" }).waitFor();

    await page.setViewportSize({ width: 390, height: 844 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    if (overflow) throw new Error("Plugin UI overflows at 390px");
    await page.screenshot({ path: path.join(output, "plugins-mobile.png"), fullPage: true });
    if (errors.length) throw new Error(`Browser errors: ${JSON.stringify(errors)}`);
    console.log(JSON.stringify({ readmeXssSafe: true, configModal: true, lifecycle: true, anySearchApiV2: { installedFromBuiltMtp: true, dummySecretConfigured: true, toolRegistrationVisible: true, enabled: true, disabled: true, reenabled: true, reloaded: true, outboundApiCalls: "blocked by local proxy; search tool not invoked" }, mobileWidth: 390, consoleAndNetworkErrors: 0, screenshots: ["plugins-desktop.png", "plugin-readme.png", "plugin-settings.png", "plugins-mobile.png"] }));
  } finally {
    if (browser) await browser.close();
    backend.kill();
    if (process.exitCode) console.error(log);
  }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
