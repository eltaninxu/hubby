// Chat + pot. No framework, no build step.

const form = document.getElementById("composer");
const input = document.getElementById("text");
const send = document.getElementById("send");
const messages = document.getElementById("messages");
const potValue = document.getElementById("pot-value");
const fundForm = document.getElementById("fund");
const fundInput = document.getElementById("fund-amount");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollDown() {
  messages.scrollTop = messages.scrollHeight;
}

function addUser(text) {
  const row = el("div", "msg user");
  row.appendChild(el("div", "bubble", text));
  messages.appendChild(row);
  scrollDown();
}

function addThinking() {
  const row = el("div", "msg hubby");
  row.appendChild(el("div", "bubble muted", "checking the rules and the pot…"));
  messages.appendChild(row);
  scrollDown();
  return row;
}

function money(amount) {
  if (amount === null || amount === undefined) return "";
  return "$" + Number(amount).toFixed(2);
}

function setPot(value) {
  if (value === null || value === undefined) return;
  if (!potValue) return;
  potValue.textContent = money(value);
  potValue.dataset.pot = Number(value).toFixed(2);
}

function payoutLine(result) {
  const line = el("div", "payout");
  if (result.status === "released") {
    line.classList.add("ok");
    const ref = result.payout_item_id
      ? " · payout " + result.payout_item_id.slice(0, 8) + "…"
      : "";
    line.textContent = "Released " + money(result.amount) + " to you" + ref;
  } else if (result.status === "pending") {
    line.classList.add("wait");
    line.textContent = "PayPal is still processing this payout. It is no longer in the pot.";
  } else if (result.status === "unclaimed") {
    line.classList.add("wait");
    line.textContent = "Sent, but the recipient has no PayPal account yet for that email.";
  } else if (result.status === "failed") {
    line.classList.add("bad");
    line.textContent = "Payout failed: " + (result.error || "PayPal said no");
  } else {
    return null;
  }
  return line;
}

function addVerdict(result) {
  const row = el("div", "msg hubby");
  const bubble = el("div", "bubble verdict");

  const head = el("div", "head");
  head.appendChild(el("span", "pill " + result.decision, result.decision));
  const detail = [result.item, money(result.amount), result.category]
    .filter(Boolean)
    .join(" · ");
  if (detail) head.appendChild(el("span", "amount", detail));
  bubble.appendChild(head);

  bubble.appendChild(el("div", "reason", result.reason || ""));

  if (result.decision === "approve") {
    const line = payoutLine(result);
    if (line) bubble.appendChild(line);
  }

  row.appendChild(bubble);
  messages.appendChild(row);
  scrollDown();
  setPot(result.pot);
}

function addError(message) {
  const row = el("div", "msg hubby");
  const bubble = el("div", "bubble");
  bubble.style.background = "var(--bad-bg)";
  bubble.style.color = "var(--bad)";
  bubble.textContent = message;
  row.appendChild(bubble);
  messages.appendChild(row);
  scrollDown();
}

function addNote(message) {
  const row = el("div", "msg hubby");
  row.appendChild(el("div", "bubble muted", message));
  messages.appendChild(row);
  scrollDown();
}

async function sendRequest(text) {
  addUser(text);
  input.value = "";
  input.disabled = true;
  send.disabled = true;
  const thinking = addThinking();

  try {
    const response = await fetch("/api/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await response.json();
    thinking.remove();
    if (!response.ok) {
      addError(data.error || "Something went wrong.");
    } else {
      addVerdict(data);
    }
  } catch (err) {
    thinking.remove();
    addError("Could not reach Hubby: " + err.message);
  } finally {
    input.disabled = false;
    send.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  sendRequest(text);
});

// -- funding the pot -------------------------------------------------------
let pollTimer = null;

function pollPot(startValue) {
  clearInterval(pollTimer);
  let ticks = 0;
  pollTimer = setInterval(async () => {
    ticks += 1;
    try {
      const response = await fetch("/api/pot");
      const data = await response.json();
      if (Number(data.pot).toFixed(2) !== Number(startValue).toFixed(2)) {
        clearInterval(pollTimer);
        window.location.reload();
        return;
      }
      setPot(data.pot);
    } catch (err) {
      /* keep polling; the next tick may work */
    }
    if (ticks > 80) clearInterval(pollTimer);
  }, 3000);
}

if (fundForm) {
  fundForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const amount = Number(fundInput.value);
    if (!amount || amount <= 0) {
      addError("Pick an amount to put in the pot.");
      return;
    }
    fundForm.querySelector("button").disabled = true;
    try {
      const response = await fetch("/api/fund", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount }),
      });
      const data = await response.json();
      if (!response.ok) {
        addError(data.error || "Could not start funding.");
        return;
      }
      window.open(data.approve_url, "_blank", "noopener");
      addNote("PayPal window opened. Finish the payment, and the pot updates by itself.");
      pollPot(potValue ? potValue.dataset.pot : data.pot);
    } catch (err) {
      addError("Could not start funding: " + err.message);
    } finally {
      fundForm.querySelector("button").disabled = false;
    }
  });
}
