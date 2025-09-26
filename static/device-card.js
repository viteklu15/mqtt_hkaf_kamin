// static/device-card.js
const api = {
  power: (id, on) => fetch(`/api/device/${encodeURIComponent(id)}/power`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ on: !!on })
  }),
  set: (id, key, value) => fetch(`/api/device/${encodeURIComponent(id)}/set`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [key]: value })
  }),
  rename: (id, title) => fetch(`/api/device/${encodeURIComponent(id)}/rename`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title })
  }),
  remove: (id) => fetch(`/api/device/${encodeURIComponent(id)}`, { method: 'DELETE' }),
};

const REFRESH_INTERVAL = 30000;
const deviceCards = new Map();
let refreshTimerId = null;
let refreshInFlight = false;
const STREAM_RETRY_DELAY = 5000;
let deviceStream = null;
let deviceStreamReconnectTimer = null;

function boolFromValue(value, defaultValue = false) {
  if (value === undefined || value === null) {
    return defaultValue;
  }
  if (typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    return value !== 0;
  }
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return defaultValue;
    if (['0', 'false', 'off', 'no'].includes(normalized)) return false;
    if (['1', 'true', 'on', 'yes'].includes(normalized)) return true;
    return defaultValue;
  }
  return Boolean(value);
}

function formatTemperature(value) {
  if (value === undefined || value === null) return null;
  const num = Number(value);
  if (!Number.isFinite(num)) return null;
  const rounded = Math.round(num * 10) / 10;
  if (Math.abs(rounded - Math.round(rounded)) < 1e-6) {
    return String(Math.round(rounded));
  }
  return rounded.toFixed(1);
}

function formatTimeLeft(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'number' && Number.isFinite(value)) {
    const totalSeconds = Math.max(0, Math.floor(value));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed ? trimmed : null;
  }
  return null;
}

function updateSelectValue(select, rawValue) {
  if (!select || rawValue === undefined || rawValue === null) return;
  const value = String(rawValue);
  if (select.value === value) return;
  let hasOption = false;
  for (const option of select.options) {
    if (option.value === value) {
      hasOption = true;
      break;
    }
  }
  if (!hasOption) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    option.hidden = true;
    select.appendChild(option);
  }
  select.value = value;
}

function updatePowerButton(button, power, online) {
  if (!button) return;
  button.setAttribute('aria-pressed', power ? 'true' : 'false');
  button.toggleAttribute('disabled', !online);
  button.classList.toggle('opacity-50', !online);
  button.classList.toggle('pointer-events-none', !online);
  const icon = button.querySelector('svg');
  if (icon) {
    icon.classList.remove('text-emerald-400', 'text-neutral-400');
    icon.classList.add(online && power ? 'text-emerald-400' : 'text-neutral-400');
  }
}

/* ====== НОВОЕ: универсальная окраска + установка значения ====== */
function setValueAndColor(el, { online, power, value, suffix }) {
  if (!el) return;
  el.classList.remove('text-emerald-400', 'text-neutral-500', 'text-neutral-700');

  if (online && power) {
    el.classList.add('text-emerald-400');
  } else if (online && !power) {
    el.classList.add('text-neutral-500');
  } else {
    el.classList.add('text-neutral-700'); // offline — темнее
  }

  if (online && power && value != null && value !== '') {
    el.textContent = suffix ? `${value}${suffix}` : `${value}`;
  } else {
    el.textContent = '—';
  }
}

/* НОВОЕ: чтение текущих значений из DOM для мгновенного UI после клика */
function getDisplayedMetrics(card) {
  const tempText = card.querySelector('[data-temp]')?.textContent?.trim();
  const timeText = card.querySelector('[data-time]')?.textContent?.trim();

  let temp_c = null;
  if (tempText && tempText !== '—') {
    const num = Number(tempText.replace(/[^\d.,-]/g, '').replace(',', '.'));
    temp_c = Number.isFinite(num) ? num : null;
  }

  let time_left = null;
  if (timeText && timeText !== '—') {
    time_left = timeText;
  }
  return { temp_c, time_left };
}

/* ====== ИСПРАВЛЕНО: теперь учитываем online при отрисовке сушилки ====== */
function updateDryerCard(card, device, { power, online }) {
  const state = device.state || {};

  const tempSource = device.temp_c ?? state.temp_c;
  const formattedTemp = formatTemperature(tempSource);
  const tempEl = card.querySelector('[data-temp]');
  setValueAndColor(tempEl, {
    online,
    power,
    value: formattedTemp,
    suffix: '°C'
  });

  const timeSource = device.time_left ?? state.time_left;
  const formattedTime = formatTimeLeft(timeSource);
  const timeEl = card.querySelector('[data-time]');
  setValueAndColor(timeEl, {
    online,
    power,
    value: formattedTime,
    suffix: ''
  });
}

function updateFireplaceCard(card, device) {
  const state = device.state || {};
  const modeSelect = card.querySelector('select[data-select="mode"]');
  updateSelectValue(modeSelect, device.mode ?? state.mode);
  const soundSelect = card.querySelector('select[data-select="sound"]');
  updateSelectValue(soundSelect, device.sound ?? state.sound);
}

function updateDeviceCard(card, device) {
  if (!card || !device) return;
  const kind = (card.dataset.deviceKind || device.kind || '').toLowerCase();
  const state = device.state || {};
  const online = Boolean(device.online);

  const titleEl = card.querySelector('[data-device-title]');
  if (titleEl && device.name) {
    titleEl.textContent = device.name;
  }

  const indicator = card.querySelector('[data-online-indicator]');
  if (indicator) {
    indicator.classList.toggle('bg-emerald-400', online);
    indicator.classList.toggle('bg-red-500', !online);
  }
  const rawSerial = typeof device.serial === 'string' ? device.serial.trim().toUpperCase() : '';
  if (rawSerial) {
    card.dataset.deviceSerial = rawSerial;
  } else {
    delete card.dataset.deviceSerial;
  }
  const onlineText = card.querySelector('[data-online-text]');
  if (onlineText) {
    const statusText = online ? 'онлайн' : 'оффлайн';
    const suffix = rawSerial ? ` · SN:${rawSerial}` : '';
    onlineText.textContent = `${statusText}${suffix}`;
    onlineText.classList.remove('text-neutral-400', 'text-neutral-600');
    onlineText.classList.add(online ? 'text-neutral-400' : 'text-neutral-600');
  }

  card.querySelectorAll('[data-online-wrapper]').forEach((wrapper) => {
    wrapper.classList.toggle('opacity-50', !online);
    wrapper.classList.toggle('pointer-events-none', !online);
  });

  const power = boolFromValue(
    device.on ?? state.on ?? device.power,
    card.querySelector('[data-power]')?.getAttribute('aria-pressed') === 'true'
  );

  card.querySelectorAll('[data-power]').forEach((btn) => updatePowerButton(btn, power, online));

  card.querySelectorAll('select[data-select]').forEach((select) => {
    const key = select.dataset.select;
    let rawValue = null;
    if (key === 'mode') {
      rawValue = device.mode ?? state.mode;
    } else if (key === 'sound' && kind === 'fireplace') {
      rawValue = device.sound ?? state.sound;
    } else if (key === 'program') {
      rawValue = device.program ?? state.program;
    } else if (state && Object.prototype.hasOwnProperty.call(state, key)) {
      rawValue = state[key];
    }
    updateSelectValue(select, rawValue);
    select.disabled = !online;
    select.classList.toggle('opacity-50', !online);
    select.classList.toggle('pointer-events-none', !online);
  });

  card.querySelectorAll('input[type="checkbox"][data-toggle]').forEach((checkbox) => {
    const key = checkbox.dataset.toggle;
    let rawValue = null;
    if (key === 'backlight') {
      rawValue = device.backlight ?? state.backlight;
    } else if (key === 'sound') {
      rawValue = device.sound ?? state.sound;
    } else if (state && Object.prototype.hasOwnProperty.call(state, key)) {
      rawValue = state[key];
    } else if (key && Object.prototype.hasOwnProperty.call(device, key)) {
      rawValue = device[key];
    }
    checkbox.checked = boolFromValue(rawValue, checkbox.checked);
    checkbox.disabled = !online;
  });

  if (kind === 'dryer') {
    updateDryerCard(card, device, { power, online });
  } else if (kind === 'fireplace') {
    updateFireplaceCard(card, device);
  }
}

function stopDevicePolling() {
  if (refreshTimerId) {
    clearTimeout(refreshTimerId);
    refreshTimerId = null;
  }
}

function closeDeviceStream() {
  if (deviceStream) {
    deviceStream.close();
    deviceStream = null;
  }
  if (deviceStreamReconnectTimer) {
    clearTimeout(deviceStreamReconnectTimer);
    deviceStreamReconnectTimer = null;
  }
}

function scheduleStreamReconnect() {
  if (deviceStreamReconnectTimer) return;
  deviceStreamReconnectTimer = setTimeout(() => {
    deviceStreamReconnectTimer = null;
    setupDeviceStream();
  }, STREAM_RETRY_DELAY);
}

function setupDeviceStream() {
  if (!deviceCards.size) {
    closeDeviceStream();
    return;
  }
  if (!('EventSource' in window)) {
    return;
  }
  if (deviceStream) {
    return;
  }
  try {
    const source = new EventSource('/api/devices/stream');
    source.onmessage = (event) => {
      if (!event.data) return;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        console.warn('Некорректные данные потока устройств', err);
        return;
      }
      const device = payload?.device || payload;
      if (!device || !device.device_id) return;
      const card = deviceCards.get(device.device_id);
      if (card) {
        updateDeviceCard(card, device);
      }
    };
    source.addEventListener('error', () => {
      if (source.readyState === EventSource.CLOSED) {
        closeDeviceStream();
        scheduleStreamReconnect();
      }
    });
    deviceStream = source;
  } catch (err) {
    console.warn('Не удалось подключиться к потоку устройств', err);
    scheduleStreamReconnect();
  }
}

function scheduleNextRefresh(delay = REFRESH_INTERVAL) {
  if (!deviceCards.size) {
    stopDevicePolling();
    return;
  }
  if (refreshTimerId) {
    clearTimeout(refreshTimerId);
  }
  refreshTimerId = setTimeout(() => {
    runRefresh();
  }, delay);
}

async function runRefresh() {
  if (refreshInFlight || !deviceCards.size) return;
  if (refreshTimerId) {
    clearTimeout(refreshTimerId);
    refreshTimerId = null;
  }

  refreshInFlight = true;
  let nextDelay = REFRESH_INTERVAL;
  let shouldSchedule = true;

  try {
    const response = await fetch('/api/devices', { headers: { Accept: 'application/json' } });
    if (response.status === 401 || response.status === 403) {
      shouldSchedule = false;
      stopDevicePolling();
      return;
    }
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json().catch(() => ({}));
    if (!payload || payload.ok === false || !Array.isArray(payload.devices)) {
      throw new Error('Bad payload');
    }

    payload.devices.forEach((device) => {
      if (!device || !device.device_id) return;
      const card = deviceCards.get(device.device_id);
      if (card) {
        updateDeviceCard(card, device);
      }
    });
  } catch (err) {
    console.error('Не удалось обновить состояние устройств', err);
    nextDelay = REFRESH_INTERVAL * 2;
  } finally {
    refreshInFlight = false;
    if (shouldSchedule) {
      scheduleNextRefresh(nextDelay);
    }
  }
}

function startDevicePolling() {
  if (!deviceCards.size) return;
  stopDevicePolling();
  runRefresh();
}

function initDeviceCards(root = document) {
  root.querySelectorAll('[data-device-id]').forEach((card) => {
    if (!card || card.dataset.deviceInit === '1') return;
    const id = card.dataset.deviceId;
    if (!id) return;
    card.dataset.deviceInit = '1';
    deviceCards.set(id, card);

    const titleEl = card.querySelector('[data-device-title]');

    card.querySelectorAll('[data-power]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        if (btn.disabled) return;
        const prev = btn.getAttribute('aria-pressed') === 'true';
        const next = !prev;

        // Мгновенно обновляем кнопку и индикаторы цвета/значения
        btn.setAttribute('aria-pressed', String(next));
        const svg = btn.querySelector('svg');
        if (svg) {
          svg.classList.remove('text-emerald-400', 'text-neutral-400');
          svg.classList.add(next ? 'text-emerald-400' : 'text-neutral-400');
        }

        // Предполагаем online=true раз кнопка доступна; берём текущие показания из DOM
        const { temp_c, time_left } = getDisplayedMetrics(card);
        updateDryerCard(card, { temp_c, time_left, state: {} }, { power: next, online: true });

        try {
          await api.power(id, next);
        } catch (e) {
          // Откат визуально при ошибке
          btn.setAttribute('aria-pressed', String(prev));
          if (svg) {
            svg.classList.remove('text-emerald-400', 'text-neutral-400');
            svg.classList.add(prev ? 'text-emerald-400' : 'text-neutral-400');
          }
          updateDryerCard(card, { temp_c, time_left, state: {} }, { power: prev, online: true });
          alert('Не удалось переключить питание');
        }
      });
    });

    card.querySelectorAll('select[data-select]').forEach((select) => {
      select.addEventListener('change', async () => {
        if (select.disabled) return;
        try {
          await api.set(id, select.dataset.select, select.value);
        } catch (err) {
          console.error(err);
          alert('Не удалось применить параметр');
        }
      });
    });

    card.querySelectorAll('input[type="checkbox"][data-toggle]').forEach((checkbox) => {
      checkbox.addEventListener('change', async () => {
        if (checkbox.disabled) return;
        try {
          await api.set(id, checkbox.dataset.toggle, checkbox.checked);
        } catch (err) {
          console.error(err);
          alert('Не удалось применить переключатель');
        }
      });
    });

    const modal = card.querySelector('[data-settings-modal]');
    const openBtn = card.querySelector('[data-open-settings]');
    const closeEls = card.querySelectorAll('[data-close-settings]');
    const form = card.querySelector('[data-settings-form]');
    const msg = card.querySelector('[data-settings-msg]');
    const delBtn = card.querySelector('[data-delete-device]');

    function openModal() {
      modal?.classList.remove('hidden');
      document.body.classList.add('no-scroll');
      if (msg) msg.textContent = '';
      if (form?.title) form.title.value = titleEl?.textContent?.trim() || '';
    }
    function closeModal() {
      modal?.classList.add('hidden');
      document.body.classList.remove('no-scroll');
    }

    openBtn?.addEventListener('click', openModal);
    closeEls.forEach((el) => el.addEventListener('click', closeModal));
    modal?.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    document.addEventListener('keydown', (e) => {
      if (!modal || modal.classList.contains('hidden')) return;
      if (e.key === 'Escape') closeModal();
    });

    form?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const title = form.title.value.trim();
      if (!title) return;
      if (msg) {
        msg.textContent = '';
        msg.className = 'text-sm mt-2';
      }
      try {
        const r = await api.rename(id, title);
        const data = await r.json().catch(() => ({}));
        if (r.ok && data.ok !== false) {
          if (titleEl) titleEl.textContent = title;
          if (msg) {
            msg.textContent = 'Сохранено';
            msg.className = 'text-sm mt-2 text-emerald-300';
          }
          setTimeout(closeModal, 600);
        } else {
          if (msg) {
            msg.textContent = data.message || 'Не удалось сохранить';
            msg.className = 'text-sm mt-2 text-rose-300';
          }
        }
      } catch {
        if (msg) {
          msg.textContent = 'Сеть/сервер недоступен';
          msg.className = 'text-sm mt-2 text-rose-300';
        }
      }
    });

    delBtn?.addEventListener('click', async () => {
      if (!confirm('Удалить устройство? Действие необратимо.')) return;
      try {
        const r = await api.remove(id);
        const data = await r.json().catch(() => ({}));
          if (r.ok && data.ok !== false) {
            closeModal();
            card.remove();
            deviceCards.delete(id);
            if (!deviceCards.size) {
              stopDevicePolling();
              closeDeviceStream();
            }
          } else {
            alert(data.message || 'Не удалось удалить устройство');
          }
      } catch {
        alert('Сеть/сервер недоступен');
      }
    });
  });
  setupDeviceStream();
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && deviceCards.size) {
    runRefresh();
  }
});

document.addEventListener('DOMContentLoaded', () => {
  initDeviceCards();
  startDevicePolling();
  setupDeviceStream();
});

export { initDeviceCards };
