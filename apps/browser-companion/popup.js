const PROVIDER_ORIGINS = [
  "https://*.ctrip.com/*",
  "https://*.qunar.com/*",
  "https://*.ly.com/*",
  "https://*.elong.com/*",
];
const DEFAULT_BRIDGE_URL = "http://127.0.0.1:8000/browser-bridge";

const bridgeUrl = document.querySelector("#bridge-url");
const bridgeToken = document.querySelector("#bridge-token");
const status = document.querySelector("#status");

async function connectionStorage() {
  const area = chrome.storage.local || chrome.storage.session;
  if (typeof area.setAccessLevel === "function") {
    await area.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  }
  return area;
}

function isMicrosoftEdgeRuntime() {
  const brands = globalThis.navigator?.userAgentData?.brands || [];
  return (
    brands.some((brand) => /Microsoft Edge/i.test(String(brand?.brand || ""))) ||
    /\bEdg(?:A|iOS)?\//.test(String(globalThis.navigator?.userAgent || ""))
  );
}

function setStatus(message, error = false) {
  status.textContent = message;
  status.style.color = error ? "#a52a3b" : "#285338";
}

function validLocalBridgeUrl(raw) {
  try {
    const parsed = new URL(raw);
    return (
      parsed.protocol === "http:" &&
      parsed.hostname === "127.0.0.1" &&
      parsed.pathname.replace(/\/+$/, "") === "/browser-bridge" &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash
    );
  } catch {
    return false;
  }
}

async function verifyBridgePairing(url, token) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    // A deliberately missing task is a read-only authentication probe:
    // valid pairing returns 404, while a wrong token returns 401.
    const response = await fetch(
      `${url}/v1/tasks/__tripchord_pairing_probe__`,
      {
        method: "GET",
        signal: controller.signal,
        headers: {
          "X-TripChord-Bridge-Token": token,
        },
      },
    );
    if (response.status === 404) {
      return;
    }
    if (response.status === 401 || response.status === 403) {
      throw new Error("配对令牌无效");
    }
    throw new Error(`本地桥返回了意外状态 ${response.status}`);
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error("连接本地桥超时");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function restoreState() {
  if (isMicrosoftEdgeRuntime()) {
    setStatus("当前为 Edge：TripChord 已停用，请改用 Chrome", true);
    return;
  }
  const storage = await connectionStorage();
  const stored = await storage.get([
    "tripchordBridgeUrl",
    "tripchordConnected",
    "tripchordPairingStatus",
  ]);
  bridgeUrl.value = stored.tripchordBridgeUrl || DEFAULT_BRIDGE_URL;
  if (stored.tripchordConnected) {
    let runtimeStatus = null;
    try {
      runtimeStatus = await chrome.runtime.sendMessage({
        type: "tripchord:status",
      });
    } catch {
      runtimeStatus = null;
    }
    const controlState = runtimeStatus && runtimeStatus.ok
      ? runtimeStatus.control_state
      : "unknown";
    if (controlState === "ready") {
      setStatus("已连接，只读轮询中");
    } else if (["accepting", "accepted_receipt_pending"].includes(controlState)) {
      setStatus("已连接，正在确认后台更新");
    } else if (["reloading", "applied_receipt_pending"].includes(controlState)) {
      setStatus("已连接，后台更新中");
    } else if (controlState === "failed") {
      setStatus("已连接，后台更新未应用，查询已安全暂停", true);
    } else {
      setStatus("已连接，正在恢复后台状态");
    }
  } else if (stored.tripchordPairingStatus === "reauth_required") {
    setStatus("本地桥已拒绝旧令牌，需重新配对", true);
  } else {
    setStatus("尚未连接");
  }
}

document.querySelector("#grant").addEventListener("click", async () => {
  if (isMicrosoftEdgeRuntime()) {
    setStatus("不再支持 Edge 实时执行，请在 Chrome 中加载扩展", true);
    return;
  }
  try {
    const granted = await chrome.permissions.request({ origins: PROVIDER_ORIGINS });
    setStatus(granted ? "三个平台查询域名已授权" : "用户未授予平台域名权限", !granted);
  } catch (error) {
    setStatus(`权限请求失败：${String(error)}`, true);
  }
});

document.querySelector("#connect").addEventListener("click", async () => {
  if (isMicrosoftEdgeRuntime()) {
    setStatus("不再支持 Edge 实时执行，请在 Chrome 中加载扩展", true);
    return;
  }
  const url = bridgeUrl.value.trim().replace(/\/+$/, "");
  const token = bridgeToken.value.trim();
  if (!validLocalBridgeUrl(url)) {
    setStatus("桥地址只能使用本机 HTTP 地址", true);
    return;
  }
  if (token.length < 32) {
    setStatus("配对令牌长度不足", true);
    return;
  }
  const permissions = await chrome.permissions.contains({ origins: PROVIDER_ORIGINS });
  if (!permissions) {
    setStatus("请先确认并授予三个平台查询域名权限", true);
    return;
  }
  try {
    setStatus("正在核验本地桥和配对令牌…");
    await verifyBridgePairing(url, token);
    const storage = await connectionStorage();
    await storage.set({
      tripchordBridgeUrl: url,
      tripchordBridgeToken: token,
      tripchordConnected: true,
      tripchordPairingStatus: "connected",
    });
    bridgeToken.value = "";
    const started = await chrome.runtime.sendMessage({ type: "tripchord:start" });
    if (!started?.ok) {
      throw new Error(started?.error || "Chrome 只读查询启动失败");
    }
    setStatus("已连接，只读轮询中");
  } catch (error) {
    const storage = await connectionStorage();
    await storage.remove([
      "tripchordBridgeToken",
      "tripchordConnected",
    ]);
    setStatus(
      `连接失败：${String(error && error.message || error)}`,
      true,
    );
  }
});

document.querySelector("#disconnect").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "tripchord:stop" });
  const storage = await connectionStorage();
  await storage.remove([
    "tripchordBridgeToken",
    "tripchordConnected",
    "tripchordPairingStatus",
  ]);
  bridgeToken.value = "";
  setStatus("已断开，令牌已从扩展私有存储清除");
});

restoreState().catch((error) => setStatus(String(error), true));
