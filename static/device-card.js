// static/device-card.js
const api = {
  power: (id, on) => fetch(`/api/device/${encodeURIComponent(id)}/power`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ on: !!on })
  }),
  set: (id, key, value) => fetch(`/api/device/${encodeURIComponent(id)}/set`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ [key]: value })
  }),
  rename: (id, title) => fetch(`/api/device/${encodeURIComponent(id)}/rename`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ title })
  }),
  remove: (id) => fetch(`/api/device/${encodeURIComponent(id)}`, { method: 'DELETE' }),
};

function initDeviceCards(root = document) {
  root.querySelectorAll('[data-device-id]').forEach(card => {
    const id = card.dataset.deviceId;
    const titleEl = card.querySelector('[data-device-title]');

    // ===== Питание =====
    card.querySelectorAll('[data-power]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const prev = btn.getAttribute('aria-pressed') === 'true';
        const next = !prev;
        btn.setAttribute('aria-pressed', String(next));
        try {
          await api.power(id, next);
          btn.querySelector('svg')?.classList.toggle('text-emerald-400', next);
          btn.querySelector('svg')?.classList.toggle('text-neutral-400', !next);
        } catch (e) {
          btn.setAttribute('aria-pressed', String(prev));
          alert('Не удалось переключить питание');
        }
      });
    });

    // ===== Списки =====
    card.querySelectorAll('select[data-select]').forEach(sel => {
      sel.addEventListener('change', async () => {
        try { await api.set(id, sel.dataset.select, sel.value); }
        catch { alert('Не удалось применить параметр'); }
      });
    });

    // ===== Тумблеры =====
    card.querySelectorAll('input[type="checkbox"][data-toggle]').forEach(chk => {
      chk.addEventListener('change', async () => {
        try { await api.set(id, chk.dataset.toggle, chk.checked); }
        catch { alert('Не удалось применить переключатель'); }
      });
    });

    // ===== Настройки (модалка) =====
    const modal = card.querySelector('[data-settings-modal]');
    const openBtn = card.querySelector('[data-open-settings]');
    const closeEls = card.querySelectorAll('[data-close-settings]');
    const form = card.querySelector('[data-settings-form]');
    const msg = card.querySelector('[data-settings-msg]');
    const delBtn = card.querySelector('[data-delete-device]');

    function openModal() {
      modal?.classList.remove('hidden');
      document.body.classList.add('no-scroll');
      msg && (msg.textContent = '');
      if (form?.title) form.title.value = titleEl?.textContent?.trim() || '';
    }
    function closeModal() {
      modal?.classList.add('hidden');
      document.body.classList.remove('no-scroll');
    }

    openBtn?.addEventListener('click', openModal);
    closeEls.forEach(el => el.addEventListener('click', closeModal));
    modal?.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    document.addEventListener('keydown', (e) => {
      if (!modal || modal.classList.contains('hidden')) return;
      if (e.key === 'Escape') closeModal();
    });

    // Сохранить имя
    form?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const title = form.title.value.trim();
      if (!title) return;
      msg.textContent = '';
      try {
        const r = await api.rename(id, title);
        const data = await r.json().catch(() => ({}));
        if (r.ok && data.ok !== false) {
          if (titleEl) titleEl.textContent = title;
          msg.textContent = 'Сохранено';
          msg.className = 'text-sm mt-2 text-emerald-300';
          setTimeout(closeModal, 600);
        } else {
          msg.textContent = data.message || 'Не удалось сохранить';
          msg.className = 'text-sm mt-2 text-rose-300';
        }
      } catch {
        msg.textContent = 'Сеть/сервер недоступен';
        msg.className = 'text-sm mt-2 text-rose-300';
      }
    });

    // Удалить устройство
    delBtn?.addEventListener('click', async () => {
      if (!confirm('Удалить устройство? Действие необратимо.')) return;
      try {
        const r = await api.remove(id);
        const data = await r.json().catch(() => ({}));
        if (r.ok && data.ok !== false) {
          closeModal();
          card.remove();
        } else {
          alert(data.message || 'Не удалось удалить устройство');
        }
      } catch {
        alert('Сеть/сервер недоступен');
      }
    });
  });
}

document.addEventListener('DOMContentLoaded', () => initDeviceCards());
export { initDeviceCards };
