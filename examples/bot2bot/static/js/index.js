async function loadXtalk() {
    try {
        return await import("../../xtalk/index.js");
    } catch (error) {
        console.warn("Failed to load local xtalk-client bundle, falling back to CDN.", error);
        return await import("https://unpkg.com/xtalk-client@latest/dist/index.js");
    }
}

const { createSession, createAudioBridge } = await loadXtalk();

const $templateAgent = document.getElementById("template-agent");
const $templateHint = document.getElementById("template-hint");
const $connectionStatus = document.getElementById("connection-status");
const $conversationStatus = document.getElementById("conversation-status");
const $runStatus = document.getElementById("run-status");
const $turnCounter = document.getElementById("turn-counter");
const $botList = document.getElementById("bot-list");
const $messages = document.getElementById("messages");
const $btnAddBot = document.getElementById("btn-add-bot");
const $btnStart = document.getElementById("btn-start");
const $btnStop = document.getElementById("btn-stop");
const $botCardTemplate = document.getElementById("bot-card-template");

const state = {
    loadingTemplate: true,
    starting: false,
    stopping: false,
    running: false,
    template: null,
    botDrafts: [],
    bridge: null,
    runId: null,
    botRecords: [],
    messageNodes: new Map(),
    finalizedMessageKeys: new Set(),
};

function cloneBotDraft(source, index) {
    return {
        name: source?.name?.trim() || `Bot ${index}`,
        system_prompt: source?.system_prompt?.trim() || "",
        proactive: Boolean(source?.proactive),
        voice: source?.voice?.trim() || "",
    };
}

function buildNewBotDraft() {
    const templateBot = state.template?.default_bots?.[state.botDrafts.length % 2] ?? null;
    const draft = cloneBotDraft(templateBot, state.botDrafts.length + 1);
    if (state.botDrafts.some((bot) => bot.proactive)) {
        draft.proactive = false;
    }
    return draft;
}

function setConnectionStatus(text) {
    $connectionStatus.textContent = text;
}

function setConversationStatus(text) {
    $conversationStatus.textContent = text;
}

function setRunStatus(text) {
    $runStatus.textContent = text;
}

function updateButtons() {
    const disableEditing = state.running || state.starting;
    $btnAddBot.disabled = disableEditing || state.loadingTemplate;
    $btnStart.disabled = disableEditing || state.loadingTemplate;
    $btnStop.disabled = !(state.running || state.stopping);
}

function updateTurnCounter() {
    const completedTurns = state.finalizedMessageKeys.size;
    $turnCounter.textContent = completedTurns > 0 ? `${completedTurns} turns finished` : "No turns yet";
}

function createMessageCard(botName, messageIndex) {
    const card = document.createElement("article");
    card.className = "message-card is-streaming";

    const head = document.createElement("div");
    head.className = "message-head";

    const name = document.createElement("div");
    name.className = "message-name";
    name.textContent = botName;

    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = `Turn ${messageIndex + 1}`;

    const text = document.createElement("p");
    text.className = "message-text";

    head.append(name, meta);
    card.append(head, text);
    $messages.appendChild(card);
    $messages.scrollTop = $messages.scrollHeight;
    return card;
}

function upsertTimelineMessage(bot, messageIndex, message) {
    const key = `${bot.id}:${messageIndex}`;
    let card = state.messageNodes.get(key);
    if (!card) {
        card = createMessageCard(bot.name, messageIndex);
        state.messageNodes.set(key, card);
    }

    card.querySelector(".message-name").textContent = bot.name;
    card.querySelector(".message-meta").textContent = `Turn ${messageIndex + 1}`;
    card.querySelector(".message-text").textContent = message.content || "";
    card.classList.toggle("is-streaming", !message.final);

    if (message.final) {
        state.finalizedMessageKeys.add(key);
    }
    updateTurnCounter();
    $messages.scrollTop = $messages.scrollHeight;
}

function clearTimeline() {
    state.messageNodes.clear();
    state.finalizedMessageKeys.clear();
    $messages.innerHTML = "";
    updateTurnCounter();
}

function renderBots() {
    $botList.innerHTML = "";
    const disableEditing = state.running || state.starting;

    state.botDrafts.forEach((bot, index) => {
        const fragment = $botCardTemplate.content.cloneNode(true);
        const card = fragment.querySelector(".bot-card");
        const title = fragment.querySelector(".bot-card-title");
        const removeButton = fragment.querySelector(".remove-btn");
        const nameInput = fragment.querySelector(".bot-name");
        const promptInput = fragment.querySelector(".bot-system-prompt");
        const proactiveInput = fragment.querySelector(".bot-proactive");
        const voiceInput = fragment.querySelector(".bot-voice");

        title.textContent = `Bot ${index + 1}`;
        removeButton.disabled = disableEditing || state.botDrafts.length <= 2;
        nameInput.value = bot.name;
        promptInput.value = bot.system_prompt;
        proactiveInput.checked = bot.proactive;
        voiceInput.value = bot.voice;

        nameInput.disabled = disableEditing;
        promptInput.disabled = disableEditing;
        proactiveInput.disabled = disableEditing;
        voiceInput.disabled = disableEditing;

        removeButton.addEventListener("click", () => {
            state.botDrafts.splice(index, 1);
            if (!state.botDrafts.some((item) => item.proactive) && state.botDrafts[0]) {
                state.botDrafts[0].proactive = true;
            }
            renderBots();
        });
        nameInput.addEventListener("input", (event) => {
            state.botDrafts[index].name = event.target.value;
        });
        promptInput.addEventListener("input", (event) => {
            state.botDrafts[index].system_prompt = event.target.value;
        });
        voiceInput.addEventListener("input", (event) => {
            state.botDrafts[index].voice = event.target.value;
        });
        proactiveInput.addEventListener("change", (event) => {
            const checked = Boolean(event.target.checked);
            state.botDrafts.forEach((item, itemIndex) => {
                item.proactive = checked && itemIndex === index;
            });
            renderBots();
        });

        $botList.appendChild(card);
    });
}

function buildWebSocketURL(path) {
    const url = new URL(path, window.location.href);
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return url;
}

async function fetchJSON(url, options = {}) {
    const response = await fetch(url, {
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
        ...options,
    });
    if (!response.ok) {
        let message = `Request failed with status ${response.status}`;
        try {
            const payload = await response.json();
            if (payload?.detail) {
                message = String(payload.detail);
            }
        } catch (_error) {
        }
        throw new Error(message);
    }
    return await response.json();
}

function validateBotDrafts() {
    if (state.botDrafts.length < 2) {
        throw new Error("At least two bots are required.");
    }
    const proactiveCount = state.botDrafts.filter((bot) => bot.proactive).length;
    if (proactiveCount !== 1) {
        throw new Error("Exactly one bot must set proactive=true.");
    }

    state.botDrafts.forEach((bot, index) => {
        if (!bot.name.trim()) {
            throw new Error(`Bot ${index + 1} name must be non-empty.`);
        }
        if (!bot.system_prompt.trim()) {
            throw new Error(`Bot ${index + 1} system prompt must be non-empty.`);
        }
    });
}

function refreshRuntimeStatus() {
    if (!state.running && !state.starting) {
        setConnectionStatus("Idle");
        setConversationStatus("Idle");
        setRunStatus("None");
        return;
    }

    if (state.starting) {
        setConnectionStatus("Connecting");
        setConversationStatus("Starting");
        setRunStatus(state.runId ? state.runId.slice(0, 8) : "Preparing");
        return;
    }

    const total = state.botRecords.length;
    const connected = state.botRecords.filter(
        (record) => record.session.state.connectionState === "connected",
    ).length;
    setConnectionStatus(`${connected}/${total} connected`);

    const speakingBot = state.botRecords.find(
        (record) => record.session.state.streamState === "speaking",
    );
    const processingBot = state.botRecords.find(
        (record) => record.session.state.streamState === "processing",
    );

    if (speakingBot) {
        setConversationStatus(`${speakingBot.meta.name} speaking`);
    } else if (processingBot) {
        setConversationStatus(`${processingBot.meta.name} processing`);
    } else {
        setConversationStatus("Running");
    }
    setRunStatus(state.runId ? state.runId.slice(0, 8) : "Active");
}

async function stopLocalRuntime() {
    const sessions = state.botRecords.map((record) => record.session);
    const bridge = state.bridge;
    const runId = state.runId;

    state.botRecords = [];
    state.bridge = null;
    state.runId = null;
    state.running = false;
    state.starting = false;
    state.stopping = false;
    updateButtons();
    refreshRuntimeStatus();
    renderBots();

    for (const session of sessions) {
        try {
            await session.close();
        } catch (_error) {
        }
    }
    if (bridge) {
        try {
            await bridge.close();
        } catch (_error) {
        }
    }
    if (runId) {
        try {
            await fetchJSON("/api/bot2bot/stop", {
                method: "POST",
                body: JSON.stringify({ run_id: runId }),
            });
        } catch (_error) {
        }
    }
}

async function startBot2Bot() {
    validateBotDrafts();

    if (state.running || state.starting) {
        return;
    }

    state.starting = true;
    updateButtons();
    clearTimeline();
    refreshRuntimeStatus();

    try {
        const payload = {
            bots: state.botDrafts.map((bot) => ({
                name: bot.name.trim(),
                system_prompt: bot.system_prompt.trim(),
                proactive: bot.proactive,
                voice: bot.voice.trim(),
            })),
        };
        const run = await fetchJSON("/api/bot2bot/start", {
            method: "POST",
            body: JSON.stringify(payload),
        });

        state.runId = run.run_id;
        state.bridge = createAudioBridge();

        const botRecords = run.bots.map((botMeta) => {
            const session = createSession(buildWebSocketURL(botMeta.websocket_path), {
                inputConfig: {
                    sampleRate: 16000,
                    mode: "web_bridge",
                    participantId: botMeta.id,
                    bridge: state.bridge,
                },
                serviceURLs: {
                    login: new URL(botMeta.login_path, window.location.href),
                    sessions: new URL(botMeta.sessions_path, window.location.href),
                    sessionDetail: (sessionId) =>
                        new URL(
                            `${botMeta.sessions_path}/${encodeURIComponent(sessionId)}`,
                            window.location.href,
                        ),
                    upload: new URL(botMeta.upload_path, window.location.href),
                },
            });

            session.onOutputAudioChunk((pcmChunkInt16, sampleRate) => {
                state.bridge?.publishAudio(pcmChunkInt16, {
                    sourceId: botMeta.id,
                    sampleRate,
                });
            });
            session.onStateChange((sessionState) => {
                const assistantMessages = sessionState.messages.filter(
                    (message) => message.role === "assistant",
                );
                assistantMessages.forEach((message, messageIndex) => {
                    upsertTimelineMessage(botMeta, messageIndex, message);
                });
                refreshRuntimeStatus();
            });

            return {
                meta: botMeta,
                session,
            };
        });

        state.botRecords = botRecords;
        renderBots();
        refreshRuntimeStatus();

        const proactiveRecord = botRecords.find((record) => record.meta.proactive);
        const passiveRecords = botRecords.filter((record) => !record.meta.proactive);

        await Promise.all(passiveRecords.map((record) => record.session.open()));
        if (proactiveRecord) {
            await proactiveRecord.session.open();
        }

        state.running = true;
        state.starting = false;
        updateButtons();
        refreshRuntimeStatus();
    } catch (error) {
        await stopLocalRuntime();
        throw error;
    }
}

async function loadTemplate() {
    state.loadingTemplate = true;
    updateButtons();
    const template = await fetchJSON("/api/bot2bot/template");
    state.template = template;
    state.botDrafts = Array.isArray(template.default_bots)
        ? template.default_bots.map((bot, index) => cloneBotDraft(bot, index + 1))
        : [cloneBotDraft(null, 1), cloneBotDraft(null, 2)];
    if (!state.botDrafts.some((bot) => bot.proactive) && state.botDrafts[0]) {
        state.botDrafts[0].proactive = true;
    }
    $templateAgent.textContent = template.agent_type || "Unknown";
    $templateHint.textContent = `Template agent: ${template.agent_type || "Unknown"}. One Xtalk.from_config instance will be created for each bot when you click Start.`;
    state.loadingTemplate = false;
    renderBots();
    updateButtons();
}

$btnAddBot.addEventListener("click", () => {
    state.botDrafts.push(buildNewBotDraft());
    renderBots();
});

$btnStart.addEventListener("click", async () => {
    try {
        await startBot2Bot();
    } catch (error) {
        alert(error?.message || String(error));
    }
});

$btnStop.addEventListener("click", async () => {
    if (!state.running) {
        return;
    }
    state.stopping = true;
    updateButtons();
    try {
        await stopLocalRuntime();
    } finally {
        state.stopping = false;
        updateButtons();
        refreshRuntimeStatus();
    }
});

try {
    await loadTemplate();
    refreshRuntimeStatus();
} catch (error) {
    console.error("Failed to load bot2bot template metadata.", error);
    $templateAgent.textContent = "Error";
    $templateHint.textContent = error?.message || String(error);
    alert(`Failed to load bot2bot template: ${error?.message || error}`);
}
