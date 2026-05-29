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
