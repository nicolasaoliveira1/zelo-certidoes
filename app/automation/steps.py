"""Execução data-driven dos steps municipais + mapa de localizadores Selenium.

Extraído de routes.py (C1). Funções puras sobre o driver Selenium, sem
dependência do estado de lote.
"""
import re
import time
import unicodedata

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

BY_MAP = {
    'id': By.ID,
    'name': By.NAME,
    'css_selector': By.CSS_SELECTOR,
    'xpath': By.XPATH,
    'class_name': By.CLASS_NAME,
}


def executar_municipio(driver, wait, steps, cnpj_limpo, inscricao_limpa, etapa_label='steps'):
    if not steps:
        return None

    def _normalizar_texto(valor):
        texto = (valor or '')
        texto = unicodedata.normalize('NFKD', texto)
        texto = ''.join(ch for ch in texto if not unicodedata.combining(ch))
        texto = re.sub(r'\s+', ' ', texto).strip().upper()
        return texto

    by_map = BY_MAP

    for idx, step in enumerate(steps, start=1):
        tipo = (step or {}).get('tipo')
        if not tipo:
            continue

        if tipo == 'click_if_text_or_close':
            by = by_map.get(step.get('by'))
            locator = step.get('locator')
            expected_text = _normalizar_texto(step.get('expected_text_contains'))
            timeout = float(step.get('timeout', 10))
            sleep_after = float(step.get('sleep', 0.5))
            wait_url_contains = (step.get('wait_url_contains') or '').strip()

            if not by or not locator or not expected_text:
                continue

            if wait_url_contains:
                try:
                    WebDriverWait(driver, timeout).until(lambda d: wait_url_contains in (d.current_url or ''))
                except TimeoutException:
                    print(f"[MUNICIPAL] Timeout aguardando URL final do step condicional ({wait_url_contains}).")

            try:
                WebDriverWait(driver, timeout).until(
                    lambda d: d.execute_script('return document.readyState') == 'complete'
                )
            except TimeoutException:
                pass

            try:
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_all_elements_located((by, locator))
                )
            except TimeoutException:
                pass

            try:
                elementos = driver.find_elements(by, locator)
            except Exception as exc_find:
                print(f"[MUNICIPAL] Erro ao buscar elementos do step condicional: {exc_find!r}")
                raise

            alvo = None
            for pos, elemento in enumerate(elementos, start=1):
                texto_variantes = [
                    _normalizar_texto(elemento.text),
                    _normalizar_texto(elemento.get_attribute('textContent')),
                    _normalizar_texto(elemento.get_attribute('innerText')),
                ]
                if any(expected_text in t for t in texto_variantes if t):
                    alvo = elemento
                    break

            if alvo is None:
                try:
                    js_click_result = driver.execute_script(
                        """
                        const expected = arguments[0];
                        const normalize = (txt) => (txt || '')
                          .normalize('NFD')
                          .replace(/[\u0300-\u036f]/g, '')
                                                    .replace(/\\s+/g, ' ')
                          .trim()
                          .toUpperCase();
                        const anchors = Array.from(document.querySelectorAll('a'));
                        for (const a of anchors) {
                          const text = normalize(a.innerText || a.textContent || '');
                          if (text.includes(expected)) {
                            a.click();
                            return {
                              clicked: true,
                              text,
                              href: a.getAttribute('href') || ''
                            };
                          }
                        }
                        return {clicked: false, count: anchors.length};
                        """,
                        expected_text
                    )
                    if js_click_result and js_click_result.get('clicked'):
                        print('[MUNICIPAL] Link de certidão NEGATIVA encontrado. Prosseguindo com download.')
                        time.sleep(sleep_after)
                        continue
                except Exception as exc_js_click:
                    print(f"[MUNICIPAL] Erro no fallback JS do step condicional: {exc_js_click!r}")

                print('[MUNICIPAL] Link de certidão NEGATIVA não encontrado. Retornando pendente.')
                try:
                    driver.get('about:blank')
                except Exception:
                    pass
                return {'encerrar_sem_arquivo': True}

            try:
                alvo.click()
            except Exception:
                driver.execute_script('arguments[0].click();', alvo)

            print('[MUNICIPAL] Link de certidão NEGATIVA encontrado. Prosseguindo com download.')
            time.sleep(sleep_after)
            continue

        if tipo == 'sleep':
            time.sleep(float(step.get('seconds', 1)))
            continue

        if tipo == 'refresh':
            driver.refresh()
            time.sleep(float(step.get('sleep', 1)))
            continue

        if tipo == 'wait_for':
            by = by_map.get(step.get('by'))
            locator = step.get('locator')
            if not by or not locator:
                continue
            timeout = step.get('timeout', 10)
            state = step.get('state', 'clickable')
            cond = EC.element_to_be_clickable if state == 'clickable' else EC.presence_of_element_located
            WebDriverWait(driver, timeout).until(cond((by, locator)))
            continue

        if tipo == 'press_tab':
            by = by_map.get(step.get('by'))
            locator = step.get('locator')
            sleep_after = float(step.get('sleep', 0.2))

            try:
                if by and locator:
                    elemento = wait.until(EC.element_to_be_clickable((by, locator)))
                else:
                    elemento = driver.switch_to.active_element
                elemento.send_keys(Keys.TAB)
                time.sleep(sleep_after)
            except Exception as exc:
                print(f"[MUNICIPAL] Aviso: falha ao enviar TAB: {exc}")
            continue

        if tipo in ['click', 'click_js', 'select', 'fill']:
            by = by_map.get(step.get('by'))
            locator = step.get('locator')
            if not by or not locator:
                continue

            elemento = wait.until(EC.element_to_be_clickable((by, locator)))

            if tipo == 'click':
                elemento.click()
                time.sleep(float(step.get('sleep', 0.5)))
                continue

            if tipo == 'click_js':
                driver.execute_script("arguments[0].click();", elemento)
                time.sleep(float(step.get('sleep', 0.5)))
                continue

            if tipo == 'select':
                select_obj = Select(elemento)
                value = step.get('value')
                text = step.get('text')
                contains = step.get('text_contains')
                if value is not None:
                    select_obj.select_by_value(value)
                elif text:
                    select_obj.select_by_visible_text(text)
                elif contains:
                    for opt in select_obj.options:
                        if contains.upper() in opt.text.upper():
                            select_obj.select_by_visible_text(opt.text)
                            break
                time.sleep(float(step.get('sleep', 0.5)))
                continue

            if tipo == 'fill':
                value = step.get('value')
                if value == 'cnpj':
                    value = cnpj_limpo
                elif value == 'inscricao':
                    value = inscricao_limpa
                if value is None:
                    continue
                elemento.clear()
                elemento.click()
                elemento.send_keys(value)
                time.sleep(float(step.get('sleep', 0.5)))
                continue
    return None
