/**
 * UTM Tracker — перехват UTM меток из Яндекс Директ и передача в AmoCRM
 *
 * Установка: вставить перед </body> на всех страницах сайта (Битрикс)
 *
 * Что делает:
 * 1. Читает UTM параметры из URL при первом заходе
 * 2. Сохраняет в cookies (живут 30 дней, не теряются при переходах)
 * 3. При отправке формы — подставляет UTM в скрытые поля
 */

(function () {
  'use strict';

  var UTM_PARAMS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'];
  var COOKIE_DAYS = 30;
  var COOKIE_PREFIX = 'utm_';

  // ========================= Cookies =========================

  function setCookie(name, value, days) {
    var expires = '';
    if (days) {
      var date = new Date();
      date.setTime(date.getTime() + days * 24 * 60 * 60 * 1000);
      expires = '; expires=' + date.toUTCString();
    }
    document.cookie = name + '=' + encodeURIComponent(value) + expires + '; path=/; SameSite=Lax';
  }

  function getCookie(name) {
    var nameEQ = name + '=';
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.indexOf(nameEQ) === 0) {
        return decodeURIComponent(c.substring(nameEQ.length));
      }
    }
    return '';
  }

  // ========================= UTM Capture =========================

  function getUrlParam(name) {
    var results = new RegExp('[?&]' + name + '=([^&#]*)').exec(window.location.search);
    return results ? decodeURIComponent(results[1]) : '';
  }

  function captureUtm() {
    var hasUtm = false;
    UTM_PARAMS.forEach(function (param) {
      var value = getUrlParam(param);
      if (value) {
        hasUtm = true;
      }
    });

    // Сохраняем только если в URL есть хотя бы одна UTM метка
    // (чтобы не затирать старые метки при переходах внутри сайта)
    if (hasUtm) {
      UTM_PARAMS.forEach(function (param) {
        var value = getUrlParam(param);
        setCookie(COOKIE_PREFIX + param, value, COOKIE_DAYS);
      });
    }
  }

  function getStoredUtm() {
    var result = {};
    UTM_PARAMS.forEach(function (param) {
      result[param] = getCookie(COOKIE_PREFIX + param);
    });
    return result;
  }

  // ========================= Form Injection =========================

  function injectHiddenFields(form) {
    var utm = getStoredUtm();
    UTM_PARAMS.forEach(function (param) {
      // Не дублируем если поле уже есть
      var existing = form.querySelector('input[name="' + param + '"]');
      if (existing) {
        existing.value = utm[param] || '';
        return;
      }
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = param;
      input.value = utm[param] || '';
      form.appendChild(input);
    });
  }

  function hookForms() {
    var forms = document.querySelectorAll('form');
    forms.forEach(function (form) {
      form.addEventListener('submit', function () {
        injectHiddenFields(form);
      });
    });

    // Наблюдатель для динамически добавленных форм (SPA, попапы Битрикс)
    var observer = new MutationObserver(function (mutations) {
      mutations.forEach(function (mutation) {
        mutation.addedNodes.forEach(function (node) {
          if (node.nodeType !== 1) return;
          // Если добавлена форма
          if (node.tagName === 'FORM') {
            node.addEventListener('submit', function () {
              injectHiddenFields(node);
            });
          }
          // Если внутри добавленного элемента есть формы
          var innerForms = node.querySelectorAll ? node.querySelectorAll('form') : [];
          innerForms.forEach(function (f) {
            f.addEventListener('submit', function () {
              injectHiddenFields(f);
            });
          });
        });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // ========================= Init =========================

  captureUtm();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hookForms);
  } else {
    hookForms();
  }

  // Глобальный доступ для ручного использования (если нужно передать UTM через AJAX)
  window.UTMTracker = {
    getUtm: getStoredUtm,
    injectFields: injectHiddenFields
  };
})();
