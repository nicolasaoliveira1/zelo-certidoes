// Sistema de toasts empilhados do dashboard.
// Extraido de dashboard.js (spec 05, REFA-03) como modulo ES autocontido:
// mantem seu proprio estado/DOM e expoe apenas showToast. Carrega o elemento
// #toastStack no import (modulo deferido: DOM ja parseado).

const toastStack = document.getElementById('toastStack');

// ---- Pilha de toasts acumulativos -------------------------------
const toasts = [];          // index 0 = mais novo (na frente)
let stackHovered = false;
let leaveTimer = null;
const PEEK = 10;            // px que cada toast de tras "espia" (recolhido)
const GAP = 9;             // espaco entre toasts (expandido)
const MAX_PEEK = 3;        // quantos toasts de tras ficam visiveis recolhidos
const MAX_TOASTS = 6;      // limite na pilha
const TOAST_DELAY = 6000;

function bgClass(type) {
    if (type === 'success') return 'bg-success';
    if (type === 'error') return 'bg-danger';
    return 'bg-primary';
}

function reflow() {
    let acumulado = 0;
    toasts.forEach((t, i) => {
        if (t.leaving) return;
        let y, escala, opacidade;
        if (stackHovered) {
            y = -acumulado;
            escala = 1;
            opacidade = 1;
            acumulado += t.el.offsetHeight + GAP;
        } else {
            const nivel = Math.min(i, MAX_PEEK);
            y = -(nivel * PEEK);
            escala = 1 - nivel * 0.05;
            opacidade = i > MAX_PEEK ? 0 : 1;
        }
        t.el.style.transform = `translateY(${y}px) scale(${escala})`;
        t.el.style.opacity = opacidade;
        t.el.style.zIndex = String(1000 - i);
        t.el.style.pointerEvents = opacidade === 0 ? 'none' : 'auto';
    });
}

function removeToast(t) {
    if (t.leaving) return;
    t.leaving = true;
    clearTimeout(t.timer);
    t.el.style.transform = 'translateX(120%)';
    t.el.style.opacity = '0';
    setTimeout(() => {
        const idx = toasts.indexOf(t);
        if (idx !== -1) toasts.splice(idx, 1);
        t.el.remove();
        reflow();
    }, 350);
}

function scheduleDismiss(t) {
    clearTimeout(t.timer);
    if (stackHovered) return;   // nao some enquanto o mouse esta na pilha
    t.timer = setTimeout(() => removeToast(t), TOAST_DELAY);
}

export function showToast(message, type = 'success') {
    if (!toastStack) return;

    const el = document.createElement('div');
    el.className = 'stk-toast ' + bgClass(type);
    el.setAttribute('role', 'alert');

    const body = document.createElement('div');
    body.className = 'stk-body';
    body.textContent = message;

    const close = document.createElement('button');
    close.className = 'stk-close';
    close.type = 'button';
    close.setAttribute('aria-label', 'Fechar');
    close.innerHTML = '&times;';

    el.appendChild(body);
    el.appendChild(close);

    // estado inicial: entra de baixo
    el.style.transform = 'translateY(24px) scale(.92)';
    el.style.opacity = '0';
    toastStack.appendChild(el);

    const t = { el, timer: null, leaving: false };
    toasts.unshift(t);
    close.addEventListener('click', () => removeToast(t));

    // remove os mais antigos se passar do limite
    while (toasts.filter((x) => !x.leaving).length > MAX_TOASTS) {
        const antigo = [...toasts].reverse().find((x) => !x.leaving);
        if (antigo) removeToast(antigo); else break;
    }

    requestAnimationFrame(reflow);
    scheduleDismiss(t);
}

if (toastStack) {
    toastStack.addEventListener('mouseenter', () => {
        clearTimeout(leaveTimer);
        stackHovered = true;
        toasts.forEach((t) => clearTimeout(t.timer));
        reflow();
    });
    toastStack.addEventListener('mouseleave', () => {
        clearTimeout(leaveTimer);
        leaveTimer = setTimeout(() => {
            stackHovered = false;
            reflow();
            toasts.forEach(scheduleDismiss);
        }, 150);   // pequena folga evita flicker ao cruzar os vaos
    });
}
