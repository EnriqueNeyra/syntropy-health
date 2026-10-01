// Settings → AI: the AIs connected for Ask (models on this computer, providers with your own key, AI apps you're signed
// in to here), which one is the default, connecting more, and access for other AI apps (MCP). Any number can be
// connected; Ask's model menu switches between all of them.
import { del, get, post, put } from "../api.js";
import { $, confirmDialog, fmtAgo, fmtDate, html, icon, modal, mount, onAction, plural, toast } from "../ui.js";
import { confirmAiProvider } from "../consent.js";
import { logo, logoId } from "../ai-logos.js";
import { openModelPicker } from "../model-picker.js";

const TOKEN_PLACEHOLDER = "YOUR_TOKEN";
const HOSTED = ["anthropic", "openai", "gemini", "openrouter", "xai", "mistral", "groq", "deepseek", "together", "fireworks", "cerebras"];
const NAME = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google Gemini", openrouter: "OpenRouter", xai: "xAI",
  mistral: "Mistral", groq: "Groq", deepseek: "DeepSeek", together: "Together AI", fireworks: "Fireworks AI", cerebras: "Cerebras",
  custom: "Other service" };
const BLURB = { anthropic: "Claude", openai: "GPT and o-series", gemini: "Gemini", openrouter: "Hundreds of models, one key",
  xai: "Grok", mistral: "Mistral and Codestral", groq: "Fast open models", deepseek: "DeepSeek V and R", together: "Open models",
  fireworks: "Open models", cerebras: "Fast open models", custom: "Any OpenAI-compatible API" };
const KEY_URL = { anthropic: "https://console.anthropic.com/settings/keys", openai: "https://platform.openai.com/api-keys",
  gemini: "https://aistudio.google.com/apikey", openrouter: "https://openrouter.ai/keys", xai: "https://console.x.ai",
  mistral: "https://console.mistral.ai/api-keys", groq: "https://console.groq.com/keys", deepseek: "https://platform.deepseek.com/api_keys",
  together: "https://api.together.ai/settings/api-keys", fireworks: "https://fireworks.ai/account/api-keys", cerebras: "https://cloud.cerebras.ai" };

const gb = (bytes) => (bytes ? `${(bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 1)} GB` : "");

function toolsBadge(tools) {
  if (tools === true) return html`<span class="badge good" title="The model looks up what it needs with read-only tools">${icon("check")} Looks things up</span>`;
  if (tools === false) return html`<span class="badge" title="This model can't use tools, so it gets a compact summary of your data instead">Answers from a summary</span>`;
  return "";
}

/** Where an AI sends what Ask gives it, in a sentence. */
function whereText(c) {
  if (c.kind === "agent") return html`${icon("globe")}<span>Questions and the records it looks up go to ${c.vendor}, on your ${c.plan} plan.</span>`;
  if (c.kind === "local") {
    return c.address?.private ? html`${icon("shieldCheck")}<span>Runs on your own hardware. Nothing leaves your network.</span>`
      : html`${icon("globe")}<span>Runs on a server you chose, at ${c.base_url}.</span>`;
  }
  return html`${icon("globe")}<span>Questions and only the records it looks up go to ${c.label}, billed to your account.</span>`;
}

// Setup for the MCP apps we've tested. The first runs Syntropy Health's MCP server directly on this computer; the rest
// connect over HTTP with a token, from this computer or any other that can reach it. [name, how, code, needs token]
function snippets(url, token, local) {
  const t = token || TOKEN_PLACEHOLDER;
  const insecure = url.startsWith("http://") && !/^http:\/\/(localhost|127\.0\.0\.1)/.test(url);
  const remoteArgs = ["-y", "mcp-remote", url, "--header", "Authorization:${SYNTROPY_AUTH}", ...(insecure ? ["--allow-http"] : [])];
  const desktopHow = "In Claude, open Settings → Developer → Edit Config. Add this to claude_desktop_config.json (keep anything "
    + "already in it), save, then quit and reopen Claude.";
  const json = (v) => JSON.stringify(v, null, 2);
  const httpServer = { url, headers: { Authorization: `Bearer ${t}` } };
  return [
    ...(local ? [["Claude Desktop", `${desktopHow} This runs on the computer with Syntropy Health: no token needed, and it can read `
      + "everyone's records.", json({ mcpServers: { "syntropy-health": local } }), false]] : []),
    ["Claude Code", "Run in a terminal. It's then available in every project; remove it with: claude mcp remove syntropy-health",
      `claude mcp add --scope user --transport http syntropy-health ${url} --header "Authorization: Bearer ${t}"`, true],
    ["Codex CLI", "Run in a terminal, then start codex. Keep the token in SYNTROPY_TOKEN (for example in your shell profile).",
      `export SYNTROPY_TOKEN="${t}"\ncodex mcp add syntropy-health --url ${url} --bearer-token-env-var SYNTROPY_TOKEN`, true],
    ["Gemini CLI", "Run in a terminal. Remove it later with: gemini mcp remove syntropy-health",
      `gemini mcp add --scope user --transport http syntropy-health ${url} --header "Authorization: Bearer ${t}"`, true],
    ["Cursor", "Add to ~/.cursor/mcp.json (every project) or .cursor/mcp.json in one project:",
      json({ mcpServers: { "syntropy-health": httpServer } }), true],
    ["VS Code", "Add to .vscode/mcp.json in a project, or run MCP: Open User Configuration for every project:",
      json({ servers: { "syntropy-health": { type: "http", ...httpServer } } }), true],
    ["Windsurf", "Add to ~/.codeium/windsurf/mcp_config.json, then refresh MCP servers in Cascade:",
      json({ mcpServers: { "syntropy-health": { serverUrl: url, headers: httpServer.headers } } }), true],
    ["LM Studio", "In LM Studio, open the Program tab → Install → Edit mcp.json and add this. Pairs well with a local model: "
      + "nothing leaves your network.", json({ mcpServers: { "syntropy-health": httpServer } }), true],
    ["Goose", "Add under extensions: in ~/.config/goose/config.yaml:",
      `extensions:\n  syntropy-health:\n    type: streamable_http\n    name: syntropy-health\n    uri: ${url}\n    headers:\n      Authorization: Bearer ${t}\n    enabled: true\n    timeout: 300`, true],
    ["Claude Desktop, another computer", `${desktopHow} Needs Node.js on that computer.`,
      json({ mcpServers: { "syntropy-health": { command: "npx", args: remoteArgs, env: { SYNTROPY_AUTH: `Bearer ${t}` } } } }), true],
    ["ChatGPT", "ChatGPT only connects to apps on a public HTTPS address that sign in with OAuth, and Syntropy Health's "
      + "tokens aren't supported there yet. To use your ChatGPT plan today, install Codex CLI and connect it under "
      + "AI apps you're signed in to.", "", false],
  ];
}

export async function renderAiTab(body, { state, redraw }) {
  const cfg = await get("/api/ai/config");
  // Connecting an AI is the owner's (it serves everyone); anyone switches between them and agrees for themselves.
  const owner = cfg.can_configure !== false;
  const [{ tokens }, { profiles }, local, agentInfo] = await Promise.all([get("/api/agents/tokens"), get("/api/profiles"),
    owner ? get("/api/agents/local-command") : null, owner ? get("/api/ai/agents", null, { fresh: true }) : { agents: [] }]);
  const url = `${location.origin}/mcp`;
  const providers = Object.fromEntries(cfg.providers.map((p) => [p.id, p]));
  let agents = agentInfo;
  let detected = null;          // local AI servers found on this computer, once the scan finishes
  let scanning = true;

  const consent = async (name) => {
    if (cfg.cloud_ack) return true;
    const ok = await confirmAiProvider(name);
    if (ok) cfg.cloud_ack = true;
    return ok;
  };
  const isDefault = (id, agent) => cfg.configured && (agent ? cfg.provider === "agent" && cfg.agent_id === agent : cfg.provider === id);

  // ------------------------------------------------------------------ the default
  const defaultCard = () => {
    if (!cfg.configured) {
      return html`<section class="card ai-default off"><span class="ai-default-logo">${icon("sparkles")}</span>
        <div class="grow"><div class="ai-default-title">No AI connected yet</div>
          <div class="small muted">${owner ? "Connect one below to start using Ask. Nothing is sent anywhere until you do."
            : "The server's owner hasn't connected an AI for Ask yet."}</div></div></section>`;
    }
    const id = logoId({ provider: cfg.provider, agent: cfg.agent_id, label: cfg.label });
    const model = cfg.kind === "agent" ? (cfg.agent_model ? cfg.agent_model[0].toUpperCase() + cfg.agent_model.slice(1) : "Default model") : cfg.model_name || cfg.model;
    return html`<section class="card ai-default">
      <span class="ai-default-logo">${logo(id, cfg.label, "", cfg.kind === "local" ? "monitor" : "globe")}</span>
      <div class="grow" style="min-width:0">
        <div class="eyebrow">Default for Ask</div>
        <button class="ai-default-pick" id="default-pick" aria-haspopup="dialog" aria-expanded="false">
          <span class="truncate">${model}</span><span class="muted truncate">${cfg.label}</span>${icon("chevronDown")}</button>
        <div class="ai-where ${cfg.kind === "local" && cfg.address?.private ? "private" : ""}">${whereText(cfg)}</div>
        ${cfg.kind !== "agent" && cfg.tools != null ? html`<div style="margin-top:8px">${toolsBadge(cfg.tools)}</div>` : ""}
      </div>
      ${owner ? html`<button class="btn btn-sm" data-action="test-ai">Test</button>` : ""}
    </section>`;
  };

  // ------------------------------------------------------------------ connected
  const connectedRows = () => {
    const rows = [];
    for (const p of cfg.providers) {
      if (!p.connected) continue;
      const meta = p.id === "local" ? `${p.base_url.replace(/^https?:\/\//, "").replace(/\/v1$/, "")}${p.saved_model ? ` · ${p.saved_model}` : ""}`
        : [p.models ? plural(p.models, "model") : null, p.id === "custom" ? p.base_url.replace(/^https?:\/\//, "") : null, "Key saved"].filter(Boolean).join(" · ");
      rows.push({ id: p.id, logo: logoId({ provider: p.id, label: p.label }), label: p.id === "custom" || p.id === "local" ? p.label : NAME[p.id] || p.label,
        meta, kind: p.id === "local" ? "local" : "key", isDefault: isDefault(p.id) });
    }
    for (const a of agents.agents || []) {
      if (!a.installed || a.signed_in === false) continue;
      rows.push({ id: `agent:${a.id}`, agent: a.id, logo: logoId({ provider: "agent", agent: a.id }), label: a.label, kind: "agent",
        meta: `${a.signed_in ? "Signed in" : "Installed"} on this computer · ${a.plan} plan`, isDefault: isDefault(null, a.id) });
    }
    return rows;
  };

  const connectedCard = () => {
    const rows = connectedRows();
    if (!rows.length) return "";
    return html`<section class="card set-card"><header><h3>Connected</h3>
        <p>Ask can use any of these. Switch between them from its model menu; the default is what a new chat starts with.</p></header>
      <div class="list" style="border-top:1px solid var(--border)">${rows.map((r) => html`<div class="list-item ai-conn">
        <span class="ai-conn-logo">${logo(r.logo, r.label, "", r.kind === "local" ? "monitor" : "globe")}</span>
        <div class="grow" style="min-width:0"><div class="row" style="gap:8px"><b class="truncate">${r.label}</b>
          ${r.isDefault ? html`<span class="badge accent">Default</span>` : ""}
          ${r.kind === "local" ? html`<span class="badge good">Private</span>` : ""}</div>
          <div class="meta truncate">${r.meta}</div></div>
        ${owner ? html`<div class="row" style="gap:6px;flex:none">
          ${r.isDefault ? "" : html`<button class="btn btn-sm btn-ghost hide-narrow" data-action="make-default" data-id="${r.id}">Make default</button>`}
          <button class="btn btn-sm" data-action="manage" data-id="${r.id}">Manage</button></div>` : ""}
      </div>`)}</div></section>`;
  };

  // ------------------------------------------------------------------ connecting more
  const tile = (action, id, logoKey, title, sub, badge = "") => html`<button class="ai-tile" data-action="${action}" data-id="${id}">
    <span class="ai-tile-logo">${logo(logoKey, title, "", action === "connect-local" ? "monitor" : "globe")}</span>
    <span class="grow" style="min-width:0"><span class="ai-tile-title truncate">${title}</span><span class="ai-tile-sub truncate">${sub}</span></span>
    ${badge}</button>`;

  const addCard = () => {
    const servers = detected?.servers || [];
    const localTiles = [
      ...servers.map((s) => tile("connect-local", s.base_url, logoId({ provider: "local", label: s.name }), s.name,
        s.models.length ? `${plural(s.models.length, "model")} on this computer` : "Running, no models yet",
        s.base_url === providers.local.base_url && providers.local.connected ? html`<span class="badge good">${icon("check")}</span>` : html`<span class="badge good">Found</span>`)),
      tile("connect-local", "", servers.length ? "" : "ollama", servers.length ? "Another server" : "Ollama, LM Studio…",
        servers.length ? "On your network, or needs a key" : scanning ? "Looking on this computer…" : "None running here yet"),
    ];
    const agentTiles = (agents.agents || []).map((a) => tile("connect-agent", a.id, logoId({ provider: "agent", agent: a.id }), a.label,
      `${a.vendor} · your ${a.plan} plan`,
      !a.installed ? html`<span class="badge">Not installed</span>` : a.signed_in === false ? html`<span class="badge warn">Sign in</span>`
        : html`<span class="badge good">${icon("check")}</span>`));
    const keyTiles = [...HOSTED, "custom"].map((id) => tile("connect-key", id, id === "custom" ? logoId({ provider: "custom", label: providers.custom.label }) : id,
      id === "custom" && providers.custom.connected ? providers.custom.label : NAME[id], BLURB[id],
      providers[id].connected ? html`<span class="badge good">${icon("check")}</span>` : ""));
    return html`<section class="ai-add">
      <div class="ai-section-head"><h3>Connect an AI</h3><p>Each says where your questions and records go. Connect as many as you like.</p></div>
      <div class="ai-add-group"><div class="ai-add-head">${icon("shieldCheck")} On your computer <span>Nothing leaves your network</span>
          <button class="link-btn small" data-action="scan" ${scanning ? "disabled" : ""}>${scanning ? "Looking…" : "Scan again"}</button></div>
        <div class="ai-tiles">${localTiles}</div></div>
      ${agents.available ? html`<div class="ai-add-group"><div class="ai-add-head">${icon("code")} AI apps you're signed in to <span>Uses your existing plan</span>
          <button class="link-btn small" data-action="agents-refresh">Check again</button></div>
        <div class="ai-tiles">${agentTiles}</div></div>` : ""}
      <div class="ai-add-group"><div class="ai-add-head">${icon("key")} With your own API key <span>Billed to your account</span></div>
        <div class="ai-tiles">${keyTiles}</div></div>
    </section>`;
  };

  // ------------------------------------------------------------------ other AI apps (MCP)
  const appsCard = () => html`<section class="card set-card">
    <header class="row between wrap" style="gap:12px;align-items:flex-start">
      <div style="flex:1;min-width:240px"><h3>Other AI apps</h3>
        <p>Claude Desktop, Cursor, VS Code and other apps that support MCP can read your records with the same read-only tools as
          Ask. Every lookup is recorded in the access log.</p></div>
      <button class="btn btn-sm btn-primary" data-action="connect-app">${icon("plus")} Connect an app</button></header>
    ${tokens.length ? html`<div class="list" style="border-top:1px solid var(--border)">${tokens.map((t) => html`<div class="list-item">
        <span class="ai-token-icon">${icon("key")}</span>
        <div class="grow" style="min-width:0"><b>${t.name}</b><div class="meta">${t.profile_name ? `Only ${t.profile_name}` : "Everyone you can see"} ·
          ${t.last_call ? html`last looked up ${t.last_call.replaceAll("_", " ")} ${fmtAgo(t.last_call_at)}`
            : t.last_used_at ? `connected ${fmtAgo(t.last_used_at)}` : `created ${fmtDate(t.created_at)}, not connected yet`}</div></div>
        <button class="btn btn-sm btn-ghost" data-action="check-tokens" title="See the latest lookup">${icon("sync")}</button>
        <button class="btn btn-sm btn-ghost btn-danger" data-action="revoke" data-id="${t.id}" data-name="${t.name}">Revoke</button></div>`)}</div>`
      : html`<p class="small muted" style="padding:0 18px 16px">No apps connected yet.</p>`}
  </section>`;

  // Someone who isn't the owner: their own agreement to an online AI.
  const memberCard = () => html`<section class="card set-card"><header><h3>Your records and online AI</h3>
      <p>${cfg.cloud_ack ? "You've agreed that an AI outside your network may see your questions and the records Ask looks up. The people you share with can ask about you with it too."
        : "Before Ask sends your questions and records to an AI outside your network, it asks you once. Until you agree, others you share with can only ask about you with a local model."}</p></header>
    <div class="set-foot small muted">The server's owner chooses which AIs are connected. Switch between them from Ask's model menu.</div></section>`;

  const draw = () => mount(body, html`<div class="stack ai-settings">
    ${defaultCard()}
    ${owner ? html`${connectedCard()}${addCard()}` : memberCard()}
    ${appsCard()}</div>`);
  draw();

  const scan = async () => {
    scanning = true;
    draw();
    try { detected = await get("/api/ai/local/detect", null, { fresh: true }); } catch { detected = { servers: [] }; }
    scanning = false;
    draw();
  };
  if (owner) scan();

  /** Makes a connected AI the default for Ask. */
  const choose = async (choice, label) => {
    const { provider, agent, model } = choice;
    // A local model on the home network needs no agreement; one on the internet is confirmed when Ask first uses it.
    if (provider !== "local" && !(await consent(label))) return;
    try {
      await put("/api/ai/config", provider === "agent" ? { provider, agent, model: model ?? undefined } : { provider, model });
      toast(`${label} is now the default`, "good");
    } catch (err) { toast(err.message, "bad"); return; }
    redraw();
  };

  const opened = { key: (id) => connectKey({ id, cfg, providers, consent, done: redraw }),
    local: (address) => connectLocal({ address, cfg, detected, consent, done: redraw }),
    agent: (id) => connectAgent({ agent: agents.agents.find((a) => a.id === id), cfg, consent, done: redraw,
      refresh: async () => { agents = await get("/api/ai/agents", { refresh: true }, { fresh: true }); draw(); return agents.agents.find((a) => a.id === id); } }) };

  body.addEventListener("click", (e) => {
    const pickBtn = e.target.closest("#default-pick");
    if (!pickBtn) return;
    openModelPicker(pickBtn, { onPick: (o, g) => choose(o, g.label) });
  });

  return onAction(body, {
    scan,
    "connect-key": ({ id }) => opened.key(id),
    "connect-local": ({ id }) => opened.local(id),
    "connect-agent": ({ id }) => opened.agent(id),
    manage: ({ id }) => (id.startsWith("agent:") ? opened.agent(id.slice(6)) : id === "local" ? opened.local(providers.local.base_url) : opened.key(id)),
    "make-default": ({ id }) => {
      if (id.startsWith("agent:")) return choose({ provider: "agent", agent: id.slice(6), model: null }, agents.agents.find((a) => a.id === id.slice(6))?.label);
      const p = providers[id];
      return choose({ provider: id, model: p.saved_model || p.default_model }, id === "local" || id === "custom" ? p.label : NAME[id]);
    },
    "agents-refresh": async () => { agents = await get("/api/ai/agents", { refresh: true }, { fresh: true }); draw(); },
    "test-ai": async (_, btn) => {
      btn.disabled = true;
      btn.textContent = "Testing…";
      try {
        const r = await post("/api/ai/test");
        toast(cfg.kind === "agent" ? `Working: ${r.model} replied in ${r.seconds}s.`
          : r.tools ? `Working: ${r.model} can look things up (${r.seconds}s).`
            : `Working: ${r.model} answers from a summary of your data (${r.seconds}s).`, "good");
        if (cfg.kind !== "agent" && r.tools !== cfg.tools) return redraw();
      } catch (err) { toast(err.message, "bad"); }
      btn.disabled = false;
      btn.textContent = "Test";
    },
    "connect-app": () => connectApp({ url, local, profiles, state, onToken: (t) => { tokens.unshift(t); draw(); } }),
    "check-tokens": async () => {
      const fresh = (await get("/api/agents/tokens", null, { fresh: true })).tokens;
      tokens.splice(0, tokens.length, ...fresh);
      draw();
    },
    revoke: async ({ id, name }) => {
      if (!(await confirmDialog("Revoke access", `${name} will lose access immediately.`, { confirmLabel: "Revoke", danger: true }))) return;
      await del(`/api/agents/tokens/${id}`);
      tokens.splice(tokens.findIndex((t) => t.id === id), 1);
      draw();
    },
  });
}

// ------------------------------------------------------------------ connect dialogs
/** The dialog's frame: the provider's logo and name, a line on where data goes, the form, and its buttons. */
function connectModal({ logoKey, title, lead, connected, onDisconnect, fallback = "globe" }) {
  const m = modal({
    title: "",
    body: html`<div class="ai-connect-head"><span class="ai-connect-logo">${logo(logoKey, title, "", fallback)}</span>
        <div><h2>${title}</h2>${lead ? html`<p class="small muted">${lead}</p>` : ""}</div></div>
      <div class="ai-connect-form"></div>`,
    footer: html`${connected && onDisconnect ? html`<button class="btn btn-ghost btn-danger" data-disconnect style="margin-right:auto">Disconnect</button>` : ""}
      <button class="btn" data-cancel type="button">Cancel</button>
      <button class="btn btn-primary" data-connect disabled>${connected ? "Save" : "Connect"}</button>`,
  });
  m.el.classList.add("ai-connect-modal");
  m.el.querySelector("[data-cancel]").addEventListener("click", () => m.close());
  m.el.querySelector("[data-disconnect]")?.addEventListener("click", async () => {
    if (!(await confirmDialog(`Disconnect ${title}?`, "Its key or address is removed from this computer. Chats already answered stay.",
      { confirmLabel: "Disconnect", danger: true }))) return;
    await onDisconnect();
    m.close();
  });
  const form = m.el.querySelector(".ai-connect-form");
  const connectBtn = m.el.querySelector("[data-connect]");
  return { m, form, connectBtn };
}

/** A model dropdown with readable names, the suggested (or saved) one chosen. */
function modelSelect(id, models, selected) {
  return html`<select class="input" id="${id}">${models.map((x) => html`<option value="${x.id}" ${x.id === selected ? "selected" : ""}>${x.name}${x.name !== x.id ? ` (${x.id})` : ""}${x.size ? ` · ${gb(x.size)}` : ""}</option>`)}</select>`;
}

function defaultToggle(cfg, isDefault) {
  if (!cfg.configured || isDefault) return "";
  return html`<label class="row small ai-default-toggle"><input type="checkbox" id="make-default"> Make it the default for Ask</label>`;
}

/** Your own key: paste it, pick a model from the provider's list, connect. */
function connectKey({ id, cfg, providers, consent, done }) {
  const p = providers[id];
  const custom = id === "custom";
  const isDefault = cfg.configured && cfg.provider === id;
  const name = custom ? (p.connected ? p.label : "Another service") : NAME[id];
  const { m, form, connectBtn } = connectModal({
    logoKey: custom ? logoId({ provider: "custom", label: p.label }) : id, title: p.connected ? name : `Connect ${name}`,
    lead: "Billed to your account with the provider. It receives your questions and only the records Ask looks up, which it can read but never change.",
    connected: p.connected, onDisconnect: async () => { await del(`/api/ai/providers/${id}`); toast(`${name} disconnected`); done(); },
  });
  let models = null;      // null until listed
  let suggested = "";
  let allowHttp = false;
  const draw = () => {
    const keep = { key: $("#ai-key", form)?.value || "", url: $("#custom-url", form)?.value, label: $("#custom-label", form)?.value,
      model: $("#ai-model", form)?.value, def: $("#make-default", form)?.checked };
    mount(form, html`
      ${custom ? html`<div class="grid grid-2">
          <div class="field"><label class="label" for="custom-label">Name</label>
            <input class="input" id="custom-label" value="${keep.label ?? (p.label === "Other OpenAI-compatible" ? "" : p.label)}" placeholder="e.g. Azure OpenAI" maxlength="60"></div>
          <div class="field"><label class="label" for="custom-url">Address</label>
            <input class="input" id="custom-url" value="${keep.url ?? (p.base_url || "")}" placeholder="https://api.example.com/v1"></div></div>
        <p class="hint" style="margin:-6px 0 12px">Where the service's /chat/completions lives. Azure OpenAI: https://YOUR-RESOURCE.openai.azure.com/openai/v1.</p>` : ""}
      <div class="field"><label class="label" for="ai-key">API key ${p.has_key ? html`<span class="badge good">Saved</span>` : ""}</label>
        <div class="row"><input class="input grow" id="ai-key" type="password" autocomplete="off" value="${keep.key}"
            placeholder="${p.has_key ? "Saved. Paste a new one to replace it" : custom ? "If the service needs one" : "Paste your key"}">
          <button class="btn" type="button" data-list>${models ? "Check again" : "Continue"}</button></div>
        <div class="hint">${KEY_URL[id] ? html`Get one at <a href="${KEY_URL[id]}" target="_blank" rel="noopener">${KEY_URL[id].replace(/^https:\/\//, "").split("/")[0]}</a>. ` : ""}Stored encrypted on this computer.</div></div>
      ${models ? models.length ? html`<div class="field"><label class="label" for="ai-model">Model</label>
          ${modelSelect("ai-model", models, keep.model || p.saved_model || suggested)}
          <div class="hint">Models that can use tools look things up; others answer from a summary. You can switch models in Ask any time.</div></div>
          ${allowHttp ? html`<label class="row small ai-confirm"><input type="checkbox" id="custom-http">
            This address is on the internet and uses plain HTTP. Send my questions and health data to it unencrypted anyway.</label>` : ""}
          ${defaultToggle(cfg, isDefault)}`
        : html`<div class="ai-empty">The key works, but ${name} listed no chat models.</div>` : ""}
      <p class="small muted ai-status-line" id="key-status"></p>`);
    if (keep.def) { const d = $("#make-default", form); if (d) d.checked = true; }
    connectBtn.disabled = !models?.length;
  };
  const status = (t) => { const el = $("#key-status", form); if (el) el.textContent = t; };
  const list = async () => {
    status("Checking…");
    try {
      const r = await post("/api/ai/models", { provider: id, api_key: $("#ai-key", form).value.trim() || null,
        base_url: $("#custom-url", form)?.value.trim() || null });
      models = r.models;
      suggested = r.suggested;
      draw();
      status(models.length ? `${plural(models.length, "model")} available.` : "");
    } catch (err) { models = null; draw(); status(""); toast(err.message, "bad"); }
  };
  draw();
  if (p.has_key || (custom && p.base_url)) list();
  else setTimeout(() => $("#ai-key", form)?.focus(), 30);
  form.addEventListener("click", (e) => { if (e.target.closest("[data-list]")) list(); });
  form.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.id === "ai-key") { e.preventDefault(); list(); } });
  form.addEventListener("input", (e) => { if (e.target.id === "ai-key" && models) { models = null; draw(); $("#ai-key", form).focus(); } });
  connectBtn.addEventListener("click", async () => {
    const label = custom ? ($("#custom-label", form).value.trim() || "That service") : name;
    if (!(await consent(label))) return;
    const key = $("#ai-key", form).value.trim();
    const makeDefault = !cfg.configured || isDefault || !!$("#make-default", form)?.checked;
    const body = { provider: id, model: $("#ai-model", form).value, make_default: makeDefault, ...(key ? { api_key: key } : {}),
      ...(custom ? { base_url: $("#custom-url", form).value, label: $("#custom-label", form).value, allow_public_http: !!$("#custom-http", form)?.checked } : {}) };
    connectBtn.disabled = true;
    try { await put("/api/ai/config", body); } catch (err) {
      if (/unencrypted/.test(err.message)) { allowHttp = true; draw(); }
      toast(err.message, "bad");
      connectBtn.disabled = false;
      return;
    }
    m.close();
    await afterConnect(label, makeDefault);
    done();
  });
}

/** After connecting the default: check that it answers, and whether it can use tools. */
async function afterConnect(label, isDefault) {
  if (!isDefault) { toast(`${label} connected. Choose its models from Ask's model menu.`, "good"); return; }
  toast(`${label} connected. Checking the model…`);
  try {
    const r = await post("/api/ai/test");
    toast(r.tools === false ? `${label} works. ${r.model} can't use tools, so it answers from a summary of your data.`
      : `${label} works and can look things up (${r.seconds}s).`, "good");
  } catch (err) { toast(err.message, "bad"); }
}

/** A model on this computer or another one at home: pick the server and a model. */
function connectLocal({ address, cfg, detected, consent, done }) {
  const saved = cfg.providers.find((p) => p.id === "local");
  const servers = detected?.servers || [];
  const isDefault = cfg.configured && cfg.provider === "local";
  const start = servers.find((s) => s.base_url === address) || (address ? null : servers.find((s) => s.models.length));
  const { m, form, connectBtn } = connectModal({
    logoKey: logoId({ provider: "local", label: start?.name || saved.label }), title: "A model on your computer", fallback: "monitor",
    lead: "Ollama, LM Studio, Jan, llama.cpp or vLLM, on this computer or another at home. Nothing leaves your network.",
    connected: saved.connected && (!address || address === saved.base_url),
    onDisconnect: async () => { await del("/api/ai/providers/local"); toast("Local model disconnected"); done(); },
  });
  const st = { address: start?.base_url || address || saved.base_url || "", name: start?.name || "", models: start?.models || [],
    model: saved.saved_model, private: true, needsConfirm: false, checked: Boolean(start) };
  const draw = () => {
    const keep = { url: $("#local-url", form)?.value, key: $("#local-key", form)?.value || "", def: $("#make-default", form)?.checked };
    mount(form, html`
      ${servers.length ? html`<div class="label">Found on ${detected.in_docker ? "the computer running Docker" : "this computer"}</div>
        <div class="ai-options">${servers.map((s) => html`<button class="ai-option ${s.base_url === st.address ? "selected" : ""}" data-server="${s.base_url}"
            data-name="${s.name}" ${s.models.length ? "" : "disabled"}>
            <span class="ai-radio"></span>${logo(logoId({ provider: "local", label: s.name }), s.name)}
            <span class="grow"><b>${s.name}</b> <span class="muted small">${s.base_url.replace(/^https?:\/\//, "")}</span></span>
            <span class="small muted">${s.models.length ? plural(s.models.length, "model") : "No models yet"}</span></button>`)}</div>`
        : html`<div class="ai-empty">No AI server is running on this computer. Install <b>Ollama</b> (ollama.com) or <b>LM Studio</b>
          (lmstudio.ai) and download a model, or enter another computer's address below.
          ${detected?.in_docker ? html`<br>Syntropy Health is in Docker, so it looks at host.docker.internal. On Linux, the compose file
            needs <code>extra_hosts: ["host.docker.internal:host-gateway"]</code>, and the AI server must listen on more than 127.0.0.1
            (Ollama: OLLAMA_HOST=0.0.0.0).` : ""}</div>`}
      <details class="set-details" ${servers.length && !(!st.checked && st.address) ? "" : "open"}>
        <summary>Another computer, or a server that needs a key</summary>
        <div class="grid grid-2">
          <div class="field"><label class="label" for="local-url">Address</label>
            <div class="row"><input class="input grow" id="local-url" value="${keep.url ?? st.address}" placeholder="studio.local:1234">
              <button class="btn btn-sm" type="button" data-check>Connect</button></div>
            <div class="hint">Its name or address and port. The server must accept connections from other devices.</div></div>
          <div class="field"><label class="label" for="local-key">API key <span class="small muted">(optional)</span></label>
            <input class="input" id="local-key" type="password" autocomplete="off" value="${keep.key}" placeholder="${saved.has_key ? "Saved" : "Only if the server needs one"}"></div>
        </div></details>
      ${st.models.length ? html`<div class="field" style="margin-top:12px"><label class="label" for="local-model">Model</label>
          ${modelSelect("local-model", st.models.map((x) => ({ ...x, name: x.id })), st.models.some((x) => x.id === st.model) ? st.model : st.models[0].id)}
          <div class="hint">Pick one that can use tools (Qwen 3 8B or larger, Llama 3.1 8B) so Ask can look things up.</div></div>` : ""}
      ${st.needsConfirm ? html`<label class="row small ai-confirm"><input type="checkbox" id="local-http">
        This address is on the internet and uses plain HTTP. Send my questions and health data to it unencrypted anyway.</label>` : ""}
      ${st.models.length ? defaultToggle(cfg, isDefault) : ""}
      <p class="small muted ai-status-line" id="local-status"></p>`);
    if (keep.def) { const d = $("#make-default", form); if (d) d.checked = true; }
    connectBtn.disabled = !st.models.length;
  };
  const check = async (addr, name) => {
    const s = $("#local-status", form);
    if (s) s.textContent = "Connecting…";
    try {
      const r = await post("/api/ai/local/check", { base_url: addr, api_key: $("#local-key", form)?.value || null });
      Object.assign(st, { address: r.base_url, models: r.models, name: name || r.name || (r.private ? "Local model" : "Model server"),
        private: r.private, needsConfirm: r.needs_confirmation, checked: true });
      draw();
      const s2 = $("#local-status", form);
      if (s2) s2.textContent = r.models.length ? `${st.name}: ${plural(r.models.length, "model")}` : "Connected, but no models are downloaded.";
    } catch (err) { if ($("#local-status", form)) $("#local-status", form).textContent = ""; toast(err.message, "bad"); }
  };
  draw();
  if (!start && st.address) check(st.address, saved.label);
  form.addEventListener("click", (e) => {
    const b = e.target.closest("[data-server]");
    if (b) { const s = servers.find((x) => x.base_url === b.dataset.server); Object.assign(st, { address: s.base_url, name: s.name, models: s.models, private: true, checked: true }); draw(); }
    if (e.target.closest("[data-check]")) check($("#local-url", form).value, "");
  });
  connectBtn.addEventListener("click", async () => {
    if (st.private === false && !(await consent(st.name || "That server"))) return;
    const key = $("#local-key", form)?.value;
    const makeDefault = !cfg.configured || isDefault || !!$("#make-default", form)?.checked;
    connectBtn.disabled = true;
    try {
      await put("/api/ai/config", { provider: "local", base_url: st.address, model: $("#local-model", form).value, label: st.name || "Local model",
        allow_public_http: !!$("#local-http", form)?.checked, make_default: makeDefault, ...(key ? { api_key: key } : {}) });
    } catch (err) {
      if (/unencrypted/.test(err.message)) { st.needsConfirm = true; draw(); }
      toast(err.message, "bad");
      connectBtn.disabled = false;
      return;
    }
    m.close();
    await afterConnect(st.name || "The local model", makeDefault);
    done();
  });
}

/** An AI app already on this computer (Claude Code, Codex CLI, Gemini CLI), on the person's own plan. */
function connectAgent({ agent, cfg, consent, done, refresh }) {
  const isDefault = cfg.configured && cfg.provider === "agent" && cfg.agent_id === agent.id;
  const { m, form, connectBtn } = connectModal({
    logoKey: logoId({ provider: "agent", agent: agent.id }), title: agent.label,
    lead: `Use the ${agent.plan} plan you're signed in to. Syntropy Health runs ${agent.label} in an empty folder with read-only access to your records: no shell, no files, no web.`,
  });
  let a = agent;
  const draw = () => {
    const ready = a.installed && a.signed_in !== false;
    const current = isDefault ? cfg.agent_model || "" : "";
    const models = ["", ...(a.models || [])];
    mount(form, html`
      <div class="ai-agent-status">${!a.installed ? html`<span class="badge">Not installed</span>` : a.signed_in === false ? html`<span class="badge warn">Not signed in</span>`
          : html`<span class="badge good">${icon("check")} ${a.signed_in ? "Signed in" : "Installed"}</span>`}
        <span class="small muted">${a.vendor}${a.version ? ` · version ${a.version}` : ""}</span></div>
      ${!ready ? html`<div class="ai-empty">${!a.installed ? a.install : a.sign_in}
          <div style="margin-top:8px"><button class="btn btn-sm" data-refresh>${icon("sync")} Check again</button></div></div>`
        : html`<div class="field"><label class="label" for="agent-model">Model</label>
            <select class="input" id="agent-model">${models.map((x) => html`<option value="${x}" ${x === current ? "selected" : ""}>${x ? x[0].toUpperCase() + x.slice(1) : `${a.label}'s default`}</option>`)}</select>
            <div class="hint">${a.usage}</div></div>
          ${defaultToggle(cfg, isDefault)}`}
      <details class="ai-terms"><summary>Terms and privacy</summary><p class="small muted">${a.terms}</p></details>`);
    connectBtn.disabled = !ready;
    connectBtn.textContent = isDefault ? "Save" : "Connect";
  };
  draw();
  form.addEventListener("click", async (e) => {
    if (e.target.closest("[data-refresh]")) { a = (await refresh()) || a; draw(); }
  });
  connectBtn.addEventListener("click", async () => {
    if (!(await consent(a.vendor))) return;
    const makeDefault = !cfg.configured || isDefault || !!$("#make-default", form)?.checked;
    try {
      await put("/api/ai/config", { provider: "agent", agent: a.id, model: $("#agent-model", form).value, make_default: makeDefault });
    } catch (err) { toast(err.message, "bad"); return; }
    m.close();
    toast(makeDefault ? `${a.label} is now the default for Ask. Press Test to try it.` : `${a.label} is ready. Choose it from Ask's model menu.`, "good");
    done();
  });
}

const APP_LOGO = { "Claude Desktop": "claude", "Claude Code": "claudecode", "Codex CLI": "codex", "Gemini CLI": "geminicli",
  Cursor: "cursor", Windsurf: "windsurf", "LM Studio": "lmstudio", Goose: "goose", "Claude Desktop, another computer": "claude",
  ChatGPT: "openai" };

/** Connect an app: choose it, choose whose records it may read (creating a token), then copy its setup. */
function connectApp({ url, local, profiles, state, onToken }) {
  let app = null;        // index into snippets()
  let token = null;
  const m = modal({ title: "Connect an app", wide: true, body: "" });
  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch { toast("Copy isn't available here; select the text instead.", "bad"); }
  };
  const draw = () => {
    const list = snippets(url, token?.token, local);
    if (app == null) {
      m.setBody(html`<p class="muted" style="margin-bottom:14px">Which app should read your records? It gets read-only access, and
          you can revoke it at any time.</p>
        <div class="ai-app-grid">${list.map(([name], i) => html`<button class="ai-app" data-i="${i}">${logo(APP_LOGO[name] || "", name)}<span>${name}</span></button>`)}</div>`);
      return;
    }
    const [name, how, code, needsToken] = list[app];
    const step = needsToken && !token
      ? html`<div class="grid grid-2">
          <div class="field"><label class="label" for="tok-name">Name</label>
            <input class="input" id="tok-name" value="${name}" maxlength="80"></div>
          <div class="field"><label class="label" for="tok-profile">Can read</label>
            <select class="input" id="tok-profile"><option value="">${profiles.length > 1 ? "Everyone you can see" : "Your records"}</option>
              ${profiles.map((p) => html`<option value="${p.id}" ${p.id === state.profile?.id && profiles.length > 1 ? "selected" : ""}>Only ${p.name}'s records</option>`)}</select></div></div>
        <div class="row"><button class="btn btn-primary" data-step="create">${icon("key")} Create access</button>
          <span class="small muted">Creates a token for ${name}, shown once.</span></div>`
      : html`${token ? html`<div class="banner good"><div class="grow"><b>Access created.</b> The token is filled in below and won't be shown
          again; copy the setup now.</div></div>` : ""}
        <p class="small" style="margin-bottom:8px">${how}</p>
        ${code ? html`<div class="snippet"><pre>${code}</pre><button class="btn btn-sm" data-copy>${icon("copy")} Copy</button></div>
          <p class="hint">Then ask it something like "Summarize my latest lab results". Its lookups appear under Other AI apps.</p>` : ""}
        <div class="field" style="margin-top:14px"><div class="label">Server address</div>
          <div class="row"><code class="grow">${url}</code><button class="btn btn-sm btn-ghost" data-copy-url>${icon("copy")} Copy</button></div></div>`;
    m.setBody(html`<button class="link-btn small" data-back>${icon("chevronLeft")} All apps</button>
      <h3 style="margin:10px 0 12px">${name}</h3>${step}`);
  };
  m.body.addEventListener("click", async (e) => {
    const pick = e.target.closest("[data-i]");
    if (pick) { app = Number(pick.dataset.i); draw(); return; }
    if (e.target.closest("[data-back]")) { app = null; token = null; draw(); return; }
    if (e.target.closest("[data-copy]")) { copy(snippets(url, token?.token, local)[app][2]); return; }
    if (e.target.closest("[data-copy-url]")) { copy(url); return; }
    if (e.target.closest("[data-step=create]")) {
      try {
        token = await post("/api/agents/tokens", { name: $("#tok-name", m.el).value.trim() || "AI app", profile_id: $("#tok-profile", m.el).value || null });
        onToken({ ...token, profile_name: profiles.find((p) => p.id === token.profile_id)?.name });
        draw();
      } catch (err) { toast(err.message, "bad"); }
    }
  });
  draw();
}
