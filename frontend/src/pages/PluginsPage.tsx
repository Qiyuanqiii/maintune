import { useState } from "react";
import { api } from "../api";
import type { ConsoleState } from "../useConsole";
import { MascotScene } from "../assets/theme/ThemeArt";
import { SafeMarkdown } from "../SafeMarkdown";
import { useI18n } from "../i18n";
import type { Plugin } from "../types";

export function PluginsPage({ c }: { c: ConsoleState }) {
  const { t } = useI18n();
  const { page, plugins } = c;
  const [filename, setFilename] = useState("");
  const [editing, setEditing] = useState<Record<string, Record<string, unknown>>>({});
  const [revealed, setRevealed] = useState<Record<string, string>>({});
  const [settings, setSettings] = useState<Plugin | null>(null);
  const [readme, setReadme] = useState<{ plugin: Plugin; content: string } | null>(null);
  const [pluginUi, setPluginUi] = useState<{ plugin: Plugin; url: string } | null>(null);
  const update = (plugin: Plugin, key: string, value: unknown) => setEditing((current) => ({ ...current, [plugin.id]: { ...plugin.config, ...current[plugin.id], [key]: value } }));
  const refresh = () => c.load();
  const openReadme = (plugin: Plugin) => c.act(async () => {
    const result = await api<{ content: string }>(`/plugins/${plugin.id}/readme`);
    setReadme({ plugin, content: result.content });
  });
  const openPluginUi = (plugin: Plugin) => c.act(async () => {
    const result = await api<{ url: string }>(`/plugins/${plugin.id}/ui-session`, "POST");
    setPluginUi({ plugin, url: result.url });
  });

  if (page !== "扩展") return null;
  return <div className="plugin-page">
    <section className="experimental-panel">
      <p className="eyebrow">PLUGIN API V2 · PREVIEW</p>
      <h2>{t("plugins.title")}</h2>
      <p>{t("plugins.description")}</p>
      <span className="badge warning-badge">{t("plugins.warning")}</span>
      <div className="actions">
        <input aria-label={t("plugins.packageFile")} value={filename} onChange={(event) => setFilename(event.target.value)} placeholder="example.mtp" />
        <button disabled={!filename} onClick={() => c.act(async () => { await api(`/plugins/install/${encodeURIComponent(filename)}`, "POST"); setFilename(""); await refresh(); }, t("plugins.installed"))}>{t("plugins.install")}</button>
        <button className="secondary" onClick={() => c.act(async () => { await api("/plugins/scan"); await refresh(); }, t("plugins.scanned"))}>{t("plugins.rescan")}</button>
      </div>
      <small>{t("plugins.inboxHint")}</small>
    </section>
    {!plugins.length && <section className="empty"><MascotScene variant="empty" compact/><h3>{t("plugins.empty")}</h3><p>{t("plugins.emptyHint")}</p></section>}
    <div className="plugin-list">
      {plugins.map((plugin) => <section key={plugin.id} className="plugin-card">
        <div className="plugin-card-head">
          <div className="plugin-icon" aria-hidden="true">◇</div>
          <div className="plugin-card-copy"><h2>{plugin.name}</h2><code>{plugin.id}</code></div>
          <label className="plugin-toggle"><span>{plugin.enabled ? t("common.enabled") : t("common.disabled")}</span><input aria-label={`${plugin.name} ${t("plugins.enabledState")}`} type="checkbox" checked={plugin.enabled} onChange={() => c.act(async () => { await api(`/plugins/${plugin.id}/${plugin.enabled ? "disable" : "enable"}`, "POST"); await refresh(); })}/></label>
        </div>
        <div className="plugin-status-row">
          <span className={`badge ${plugin.runtime_status}`}>{t(`plugins.runtime.${plugin.runtime_status}` as "plugins.runtime.running")}</span>
          <span className={`badge ${plugin.connection_status === "connected" ? "completed" : "pending"}`}>{t(`plugins.connection.${plugin.connection_status}` as "plugins.connection.connected")}</span>
        </div>
        <p className="plugin-description">{plugin.description}</p>
        <div className="plugin-meta"><span>v{plugin.version}</span><span>{plugin.publisher}</span>{plugin.license && <span>{plugin.license}</span>}<span>API v{plugin.api_version}</span></div>
        {plugin.error && <div className="alert error">{plugin.error}</div>}
        <div className="plugin-actions">
          {plugin.has_readme && <button className="secondary" onClick={() => openReadme(plugin)}>{t("plugins.docs")}</button>}
          {plugin.has_ui && plugin.enabled && <button className="secondary" onClick={() => openPluginUi(plugin)}>{t("plugins.openUi")}</button>}
          {Object.keys(plugin.config_schema).length > 0 && <button className="secondary" onClick={() => setSettings(plugin)}>{t("plugins.settings")}</button>}
          {plugin.api_version === 2 && plugin.registrations?.some((item) => item.kind === "tool" && Array.isArray(item.metadata.recommended_agents) && item.metadata.recommended_agents.length > 0) && <button className="secondary" onClick={() => c.act(async () => { await api(`/plugins/${plugin.id}/tools/enable-recommended`, "POST"); await refresh(); })}>{t("plugins.recommended")}</button>}
          <button className="secondary" disabled={!plugin.enabled || plugin.runtime_status !== "running"} onClick={() => c.act(async () => { await api(`/plugins/${plugin.id}/reload`, "POST"); await refresh(); }, t("plugins.reloaded"))}>{t("plugins.reload")}</button>
          <details className="plugin-more"><summary>{t("plugins.more")}</summary>
            {plugin.api_version === 1 ? <small>{t("plugins.capabilities")}: {plugin.capabilities.join(", ") || "—"} · Legacy</small> : <div className="plugin-extensions">
              <small>Runtime: {plugin.runtime_mode}</small>
              {!plugin.registrations?.length && <small>—</small>}
              {(["hook", "tool", "service", "provider", "route"] as const).map((kind) => {
                const entries = plugin.registrations?.filter((item) => item.kind === kind) || [];
                return entries.length ? <div key={kind}><strong>{kind}</strong><ul>{entries.map((item) => <li key={`${kind}-${item.name}`}><code>{item.identifier}</code>{kind === "hook" && <small> · {String(item.metadata.stability || "")}</small>}</li>)}</ul></div> : null;
              })}
            </div>}
          </details>
        </div>
      </section>)}
    </div>
    {readme && <div className="modal-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setReadme(null); }}>
      <section className="modal modal-wide plugin-dialog" role="dialog" aria-modal="true" aria-labelledby="plugin-readme-title">
        <div className="section-head"><div><p className="eyebrow">README.md</p><h2 id="plugin-readme-title">{readme.plugin.name}</h2></div><button aria-label={t("common.close")} onClick={() => setReadme(null)}>×</button></div>
        <SafeMarkdown source={readme.content}/>
      </section>
    </div>}
    {pluginUi && <div className="modal-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPluginUi(null); }}>
      <section className="modal modal-wide plugin-dialog" role="dialog" aria-modal="true" aria-labelledby="plugin-ui-title">
        <div className="section-head"><div><p className="eyebrow">{pluginUi.plugin.id}</p><h2 id="plugin-ui-title">{pluginUi.plugin.name}</h2></div><button aria-label={t("common.close")} onClick={() => setPluginUi(null)}>×</button></div>
        <iframe className="plugin-ui-frame" title={pluginUi.plugin.name} src={pluginUi.url} sandbox="allow-scripts allow-same-origin" referrerPolicy="no-referrer" />
      </section>
    </div>}
    {settings && (() => {
      const plugin = settings;
      const values = { ...plugin.config, ...editing[plugin.id] };
      return <div className="modal-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSettings(null); }}>
        <section className="modal plugin-dialog" role="dialog" aria-modal="true" aria-labelledby="plugin-settings-title">
          <div className="section-head"><div><p className="eyebrow">{plugin.id}</p><h2 id="plugin-settings-title">{t("plugins.settingsTitle")}</h2></div><button aria-label={t("common.close")} onClick={() => setSettings(null)}>×</button></div>
          <div className="status-grid plugin-connection"><div><strong>{t("plugins.instance")}</strong><small>{plugin.connected_instance || "—"}</small></div><div><strong>{t("plugins.heartbeat")}</strong><small>{plugin.last_heartbeat ? new Date(plugin.last_heartbeat * 1000).toLocaleString() : "—"}</small></div></div>
          {plugin.api_version === 2 && plugin.runtime_supported.length > 1 && <label className="field"><span>{t("plugins.runtimeMode")}</span><select disabled={plugin.enabled} value={plugin.runtime_mode} onChange={(event) => c.act(async () => { await api(`/plugins/${plugin.id}/runtime`, "PUT", { mode: event.target.value }); setSettings(null); await refresh(); })}>{plugin.runtime_supported.map((mode) => <option key={mode} value={mode}>{mode}</option>)}</select></label>}
          <div className="repo-form plugin-config-form">
            {Object.entries(plugin.config_schema).map(([key, field]) => <label className="field" key={key}><span>{field.title}{field.required ? " *" : ""}</span>
              {field.type === "boolean" ? <input type="checkbox" checked={Boolean(values[key])} onChange={(event) => update(plugin, key, event.target.checked)} /> : field.type === "select" ? <select value={String(values[key] ?? "")} onChange={(event) => update(plugin, key, event.target.value)}>{field.options.map((option) => <option key={option}>{option}</option>)}</select> : field.type === "json" ? <textarea value={typeof values[key] === "string" ? String(values[key]) : JSON.stringify(values[key] ?? {})} onChange={(event) => { try { update(plugin, key, JSON.parse(event.target.value)); } catch { update(plugin, key, event.target.value); } }} /> : <input type={field.type === "secret" ? "password" : field.type === "integer" || field.type === "number" ? "number" : "text"} value={field.type === "string_list" ? (Array.isArray(values[key]) ? values[key].join(", ") : "") : String(values[key] ?? "")} onChange={(event) => update(plugin, key, field.type === "string_list" ? event.target.value.split(",").map((item) => item.trim()).filter(Boolean) : field.type === "integer" || field.type === "number" ? Number(event.target.value) : event.target.value)} />}
              {field.description && <small>{field.description}</small>}
            </label>)}
          </div>
          <div className="plugin-subscriptions"><strong>{t("plugins.subscriptions")}</strong><small>{plugin.event_subscriptions.join(", ") || "—"}</small></div>
          {revealed[plugin.id] && <div className="alert"><strong>{t("plugins.newToken")}</strong><code>{revealed[plugin.id]}</code></div>}
          <div className="actions"><button className="primary" onClick={() => c.act(async () => { await api(`/plugins/${plugin.id}/config`, "PUT", values); setEditing((current) => ({ ...current, [plugin.id]: {} })); setSettings(null); await refresh(); }, t("common.saved"))}>{t("common.save")}</button>
            {plugin.config_schema.bridge_token?.type === "secret" && <button className="secondary" onClick={() => c.act(async () => { const result = await api<{value:string}>(`/plugins/${plugin.id}/secrets/bridge_token/regenerate`, "POST"); setRevealed((current) => ({ ...current, [plugin.id]: result.value })); await refresh(); })}>{t("plugins.regenerate")}</button>}
          </div>
        </section>
      </div>;
    })()}
  </div>;
}
