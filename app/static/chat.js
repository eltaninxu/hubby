// Chat: post one request, render the verdict. No framework, no build step.

const form = document.getElementById("composer");
const input = document.getElementById("text");
const send = document.getElementById("send");
const messages = document.getElementById("messages");

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
  const bubble = el("div", "bubble muted", "checking the rules and the ledger…");
  row.appendChild(bubble);
  messages.appendChild(row);
  scrollDown();
  return row;
}

function money(amount) {
  if (amount === null || amount === undefined) return "";
  return "$" + Number(amount).toFixed(2);
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

  if (result.approve_url) {
    const link = el("a", "pay", "Pay " + money(result.amount) + " with PayPal");
    link.href = result.approve_url;
    link.target = "_blank";
    link.rel = "noopener";
    bubble.appendChild(link);
  }

  row.appendChild(bubble);
  messages.appendChild(row);
  scrollDown();
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
