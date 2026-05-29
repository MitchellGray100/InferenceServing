function dismissMessage(button) {
  const message = button.closest(".message");
  if (message) {
    message.remove();
  }
}

function openConfirmDialog(form) {
  const message = form.dataset.confirm;
  if (!message) {
    form.submit();
    return;
  }

  const backdrop = document.createElement("div");
  backdrop.className = "confirm-backdrop";
  backdrop.innerHTML = `
    <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
      <h2 id="confirm-title">Confirm action</h2>
      <p></p>
      <div class="actions">
        <button class="danger" type="button" data-confirm-accept>Confirm</button>
        <button class="secondary" type="button" data-confirm-cancel>Cancel</button>
      </div>
    </div>
  `;
  backdrop.querySelector("p").textContent = message;
  document.body.append(backdrop);

  const close = () => backdrop.remove();
  backdrop.querySelector("[data-confirm-cancel]").addEventListener("click", close);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) {
      close();
    }
  });
  backdrop.querySelector("[data-confirm-accept]").addEventListener("click", () => {
    close();
    form.submit();
  });
}

async function copyText(button) {
  const target = document.querySelector(button.dataset.copyTarget);
  if (!target) {
    return;
  }

  const text = target.textContent.trim();
  await navigator.clipboard.writeText(text);
  const original = button.textContent;
  button.textContent = "Copied";
  window.setTimeout(() => {
    button.textContent = original;
  }, 1600);
}

function updateAutoscalingFields(select) {
  const form = select.closest("form") || document;
  const enabled = select.value === "true";
  form.querySelectorAll("[data-autoscaling-field]").forEach((field) => {
    const label = field.closest("label");
    if (!enabled) {
      if (field.value) {
        field.dataset.autoscalingValue = field.value;
      }
      field.value = "";
      field.disabled = true;
      label?.classList.add("field-disabled");
      return;
    }

    field.disabled = false;
    label?.classList.remove("field-disabled");
    field.value =
      field.dataset.autoscalingValue || field.dataset.autoscalingDefault || "";
  });
}

function startAutoSync(element) {
  const url = element.dataset.autoSyncUrl;
  const interval = Number(element.dataset.autoSyncIntervalMs || 120000);
  if (!url || !Number.isFinite(interval) || interval <= 0) {
    return;
  }

  window.setInterval(async () => {
    try {
      await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
      });
      window.location.reload();
    } catch (_error) {
      // The next interval will try again; the page still has the manual Sync action.
    }
  }, interval);
}

function chatStreamLineText(line) {
  const trimmed = line.trim();
  if (!trimmed.startsWith("data:")) {
    return "";
  }

  const payloadText = trimmed.replace(/^data:\s*/, "");
  if (!payloadText || payloadText === "[DONE]") {
    return "";
  }

  try {
    const payload = JSON.parse(payloadText);
    const choice = payload.choices?.[0];
    return choice?.delta?.content || choice?.text || "";
  } catch (_error) {
    return payloadText;
  }
}

async function streamInference(form) {
  const outputPanel = document.querySelector("[data-inference-output-panel]");
  const output = document.querySelector("[data-inference-output]");
  const fullPanel = document.querySelector("[data-inference-full-panel]");
  const fullOutput = document.querySelector("[data-inference-full-output]");
  const status = document.querySelector("[data-inference-status]");
  const submit = form.querySelector("[data-inference-submit]");
  if (!outputPanel || !output || !fullPanel || !fullOutput || !status || !submit) {
    form.submit();
    return;
  }

  const formData = new FormData(form);
  const apiKey = String(formData.get("api_key") || "");
  const body = {
    model: String(formData.get("model") || ""),
    messages: [{ role: "user", content: String(formData.get("prompt") || "") }],
    max_tokens: Number(formData.get("max_tokens") || 128),
    temperature: Number(formData.get("temperature") || 0),
    stream: true,
  };

  output.textContent = "";
  fullOutput.textContent = "";
  outputPanel.hidden = false;
  fullPanel.hidden = false;
  status.textContent = "Streaming...";
  submit.disabled = true;
  let fullHttpResponse = "";

  try {
    const response = await fetch(form.dataset.inferenceUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(body),
    });

    if (!response.ok || !response.body) {
      const errorText = await response.text();
      throw new Error(errorText || `Request failed with ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }

      const chunkText = decoder.decode(value, { stream: true });
      fullHttpResponse += chunkText;
      fullOutput.textContent = fullHttpResponse;
      fullOutput.scrollTop = fullOutput.scrollHeight;
      buffer += chunkText;
      while (buffer.includes("\n")) {
        const splitAt = buffer.indexOf("\n");
        const line = buffer.slice(0, splitAt);
        buffer = buffer.slice(splitAt + 1);
        const text = chatStreamLineText(line);
        output.textContent += text;
        output.scrollTop = output.scrollHeight;
      }
    }

    if (buffer) {
      const text = chatStreamLineText(buffer);
      output.textContent += text;
    }
    status.textContent = "Complete";
  } catch (error) {
    status.textContent = "Failed";
    output.textContent += `\n${error.message}`;
    fullOutput.textContent = fullHttpResponse;
  } finally {
    submit.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const dismiss = event.target.closest("[data-dismiss-message]");
  if (dismiss) {
    dismissMessage(dismiss);
    return;
  }

  const copy = event.target.closest("[data-copy-target]");
  if (copy) {
    copyText(copy);
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form instanceof HTMLFormElement && form.dataset.inferenceStreamForm !== undefined) {
    event.preventDefault();
    streamInference(form);
    return;
  }

  if (form instanceof HTMLFormElement && form.dataset.confirm) {
    event.preventDefault();
    openConfirmDialog(form);
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-autoscaling-toggle]")) {
    updateAutoscalingFields(event.target);
  }
});

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-autoscaling-toggle]").forEach(updateAutoscalingFields);
  document.querySelectorAll("[data-auto-sync-url]").forEach(startAutoSync);
});
