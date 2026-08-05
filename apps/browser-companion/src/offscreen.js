const KEEPALIVE_INTERVAL_MS = 15000;

function notifyBackground() {
  chrome.runtime.sendMessage({
    type: "tripchord:keepalive",
    source: "offscreen_document",
  }).catch(() => {
    // A reload can briefly remove the receiving worker. The next bounded
    // heartbeat recreates it without retaining query or user data here.
  });
}

notifyBackground();
setInterval(notifyBackground, KEEPALIVE_INTERVAL_MS);
