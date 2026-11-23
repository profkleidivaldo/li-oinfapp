#!/usr/bin/env python3
"""IA gratuita (Google Gemini) para:
  1) ORGANIZAR o texto e as figuras de cada dia numa ordem lógica (tira cabeçalhos, junta frases quebradas,
     marca perguntas/atividades/versículos, coloca cada figura perto do texto, descarta enfeites e sugere legenda);
  2) opcionalmente TRADUZIR (TRADUTOR=gemini).

Precisa do segredo GEMINI_API_KEY (grátis em https://aistudio.google.com/apikey). Sem a chave, nada disso roda
e o resto do app funciona como antes.

Segurança contra "invenção": a resposta da IA só é aceita se contiver quase todas as palavras do texto original
e quase nenhuma palavra nova; senão o dia fica como foi extraído.
"""
import base64, collections, hashlib, io, json, os, re, sys, time, urllib.error, urllib.request

BASE = os.environ.get('GEMINI_URL', 'https://generativelanguage.googleapis.com/v1beta').rstrip('/')
KEY = os.environ.get('GEMINI_API_KEY', '').strip()
GROQ_KEY = os.environ.get('GROQ_API_KEY', '').strip()
# Provedor: Groq (gpt-oss, só texto) se houver GROQ_API_KEY; senão Gemini (lê texto e imagens). Force com IA_PROVEDOR=groq|gemini.
PROVEDOR = os.environ.get('IA_PROVEDOR', '').strip() or ('groq' if GROQ_KEY else 'gemini' if KEY else '')
ATIVA = bool(GROQ_KEY if PROVEDOR == 'groq' else KEY) and PROVEDOR in ('groq', 'gemini')
PAUSA = float(os.environ.get('PAUSA_IA', '4' if PROVEDOR == 'groq' else '6'))   # segundos entre pedidos (planos grátis limitam por minuto)
GROQ_URL = os.environ.get('GROQ_URL', 'https://api.groq.com/openai/v1').rstrip('/')
GROQ_MODELOS = [m.strip() for m in os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b,openai/gpt-oss-20b').split(',') if m.strip()]
GROQ_VISAO = os.environ.get('GROQ_VISION_MODEL', '').strip()   # opcional: modelo do Groq que enxerga imagens
VE_IMAGENS = PROVEDOR == 'gemini' or (PROVEDOR == 'groq' and bool(GROQ_VISAO))   # o gpt-oss não vê imagens
CACHE_FILE = 'data/infantil/_organizado.json'

_cache = None
_modelos = None
_falhas = 0
_avisou = False


class Cota(Exception):
    pass


# ------------------------------------------------------------------ cache
def cache():
    global _cache
    if _cache is None:
        try: _cache = json.load(open(CACHE_FILE, encoding='utf8'))
        except Exception: _cache = {}
    return _cache

def salva_cache():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    json.dump(cache(), open(CACHE_FILE, 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))


# ------------------------------------------------------------------ chamada à API
def _req(url, body=None, tries=4):
    err = None
    for i in range(tries):
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json', 'x-goog-api-key': KEY})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode('utf8'))
        except urllib.error.HTTPError as e:
            msg = e.read().decode('utf8', 'ignore')[:400]
            if e.code == 429 and re.search(r'PerDay|per day|limit: 0', msg, re.I):
                raise Cota(msg)                       # cota do dia acabou: não adianta insistir
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(f'HTTP {e.code}: {msg}')
            err = f'HTTP {e.code}: {msg}'
            time.sleep(25 * (i + 1) if e.code == 429 else 5 * (i + 1))
        except Exception as e:
            err = repr(e)
            time.sleep(5 * (i + 1))
    raise RuntimeError(err)

def modelos():
    """Modelos a tentar: GEMINI_MODEL, ou os Flash mais novos que a sua chave enxerga."""
    global _modelos
    if _modelos is not None: return _modelos
    forcado = os.environ.get('GEMINI_MODEL', '').strip()
    if forcado:
        _modelos = [forcado]; return _modelos
    nomes = []
    try:
        j = _req(f'{BASE}/models?pageSize=200', tries=2)
        nomes = [m['name'].split('/')[-1] for m in j.get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
    except Exception as e:
        print('  IA: não consegui listar os modelos:', str(e)[:200], file=sys.stderr)
    def chave(n):
        m = re.fullmatch(r'gemini-(\d+(?:\.\d+)?)-flash(-lite)?', n)
        return (float(m.group(1)), 0 if m.group(2) else 1) if m else None
    cand = sorted([n for n in nomes if chave(n)], key=chave, reverse=True)
    _modelos = cand[:4] or ['gemini-2.5-flash', 'gemini-2.0-flash']
    print('  IA: modelos a tentar:', ', '.join(_modelos))
    return _modelos

def _gera_gemini(partes, sistema):
    """Envia 'partes' (texto/imagens) ao Gemini e devolve o JSON respondido (ou None)."""
    global _falhas, _avisou
    if not ATIVA: return None
    if _falhas >= 3:
        if not _avisou:
            print('  IA indisponível agora (cota/erros); o resto será feito sem IA e retomado na próxima execução.', file=sys.stderr)
            _avisou = True
        return None
    body = {'systemInstruction': {'parts': [{'text': sistema}]},
            'contents': [{'role': 'user', 'parts': partes}],
            'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2}}
    for m in list(modelos()):
        try:
            j = _req(f'{BASE}/models/{m}:generateContent', body)
            time.sleep(PAUSA)
            txt = ''.join(p.get('text', '') for p in j['candidates'][0]['content']['parts'])
            txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', txt.strip())
            res = json.loads(txt)
            _falhas = 0
            if _modelos[0] != m:                      # lembra o modelo que funcionou
                _modelos.remove(m); _modelos.insert(0, m)
            return res
        except (Cota, RuntimeError) as e:
            print(f'  IA: modelo {m} falhou:', str(e)[:160], file=sys.stderr)
            continue                                    # tenta o próximo modelo
        except (KeyError, IndexError, ValueError, TypeError) as e:
            print(f'  IA: resposta inesperada de {m}:', repr(e)[:120], file=sys.stderr)
            break
    _falhas += 1
    return None


def _req_groq(body, tries=4):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(f'{GROQ_URL}/chat/completions', data=json.dumps(body).encode(), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + GROQ_KEY, 'User-Agent': 'licao-infantil'})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode('utf8'))
        except urllib.error.HTTPError as e:
            msg = e.read().decode('utf8', 'ignore')[:500]
            if e.code == 429:
                if re.search(r'per day|\bTPD\b|\bRPD\b', msg, re.I):
                    raise Cota(msg)                          # cota do dia acabou
                m = re.search(r'try again in\s*([\d.]+)\s*(ms|s|m)\b', msg)
                esp = float(m.group(1)) * {'ms': .001, 's': 1, 'm': 60}[m.group(2)] if m else 20 * (i + 1)
                err = 'HTTP 429'
                time.sleep(min(max(esp + 1, 3), 90))
                continue
            if e.code in (400, 401, 403, 404, 413, 422):
                raise RuntimeError(f'HTTP {e.code}: {msg}')
            err = f'HTTP {e.code}: {msg}'
            time.sleep(5 * (i + 1))
        except Exception as ex:
            err = repr(ex)
            time.sleep(5 * (i + 1))
    raise RuntimeError(err)

def _gera_groq(partes, sistema):
    """Groq (OpenAI-compatível). gpt-oss é só texto; com GROQ_VISION_MODEL as imagens também são enviadas."""
    imgs = [p for p in partes if 'inline_data' in p]
    for m in ([GROQ_VISAO] if (imgs and GROQ_VISAO) else GROQ_MODELOS):
        if imgs and m == GROQ_VISAO:
            content = [{'type': 'text', 'text': p['text']} if 'text' in p else
                       {'type': 'image_url', 'image_url': {'url': f"data:{p['inline_data']['mime_type']};base64,{p['inline_data']['data']}"}}
                       for p in partes]
        else:
            content = '\n\n'.join(p['text'] for p in partes if 'text' in p)
        body = {'model': m, 'temperature': 0.2, 'max_completion_tokens': 8000,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': sistema}, {'role': 'user', 'content': content}]}
        if 'gpt-oss' in m: body['reasoning_effort'] = 'low'
        try:
            try:
                j = _req_groq(body)
            except RuntimeError as e:
                if 'HTTP 400' not in str(e): raise
                body.pop('reasoning_effort', None); body.pop('response_format', None)   # parâmetro não aceito: tenta simples
                j = _req_groq(body)
            time.sleep(PAUSA)
            txt = (j['choices'][0]['message'].get('content') or '').strip()
            txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', txt)
            try: return json.loads(txt)
            except ValueError: return json.loads(re.search(r'\{.*\}', txt, re.S).group(0))
        except (Cota, RuntimeError) as e:
            print(f'  IA (Groq): modelo {m} falhou:', str(e)[:160], file=sys.stderr)
            continue
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            print(f'  IA (Groq): resposta inesperada de {m}:', repr(e)[:120], file=sys.stderr)
            return None
    return None

def gera(partes, sistema):
    """Pergunta à IA configurada (Groq ou Gemini) e devolve o JSON respondido, ou None."""
    global _falhas, _avisou
    if not ATIVA: return None
    if _falhas >= 3:
        if not _avisou:
            print('  IA indisponível agora (cota/erros); o resto será feito sem IA e retomado na próxima execução.', file=sys.stderr)
            _avisou = True
        return None
    if PROVEDOR == 'groq':
        r = _gera_groq(partes, sistema)
        _falhas = 0 if r is not None else _falhas + 1
        return r
    return _gera_gemini(partes, sistema)


# ------------------------------------------------------------------ organizar um dia
SIS_ORG = (
    "You receive the raw items of ONE day of a children's Bible lesson (Sabbath School), extracted automatically from a "
    "PDF or web page: text fragments and pictures, possibly out of order, with broken lines, mixed columns or page artifacts. "
    "Reorganize them into a clear, logical reading order for children and teenagers.\n"
    "RULES:\n"
    "1. Do NOT add, invent, summarize or remove lesson content. Only reorder, merge fragments of the same sentence or paragraph, "
    "fix hyphenation, spacing and obvious extraction errors, and delete page headers/footers, page numbers, copyright lines and web addresses.\n"
    "2. Keep the original English wording.\n"
    "3. Give each text block a type: heading (short titles), paragraph, list_item, question (anything the child is asked to answer or discuss), "
    "activity (things to do, draw, play, write, make), verse (a memory verse or Bible quote).\n"
    "4. Put each picture next to the text it illustrates, as a block of type image with its picture id. Add a short English caption (max 12 words) "
    "only if it helps understanding, otherwise an empty caption. LEAVE OUT pictures that are only decoration (borders, backgrounds, logos, icons, page ornaments).\n"
    'Reply ONLY with JSON: {"blocks":[{"type":"heading|paragraph|list_item|question|activity|verse|image","text":"...","image":"i1","caption":"..."}]}. '
    "Text blocks use \"text\"; image blocks use \"image\" and \"caption\"."
)
# variante para IA que NÃO enxerga imagens (gpt-oss): mantém todas as figuras no lugar
R4_SEM = ("4. You CANNOT see the pictures. Keep EVERY picture as a block of type image with its picture id, placed right next to the text "
          "it most likely illustrates (usually where it originally sat), with an empty caption.\n")
SIS_ORG_SEM_VISAO = re.sub(r'4\. Put each picture.*?\n(?=Reply ONLY)', lambda m: R4_SEM, SIS_ORG, flags=re.S)
PREFIXO = {'question': '❓ ', 'activity': '✏️ ', 'verse': '📖 '}
KIND = {'h': 'heading', 'p': 'paragraph', 'li': 'list_item'}

def _toks(s): return re.findall(r"[a-z0-9']+", s.lower())

def cobre(entrada, saida):
    """True se a saída preserva o conteúdo: quase todas as palavras da entrada e quase nenhuma nova."""
    a, b = collections.Counter(_toks(entrada)), collections.Counter(_toks(saida))
    na = sum(a.values())
    if na == 0: return False
    comum = sum((a & b).values())
    return comum / na >= 0.85 and (sum(b.values()) - comum) <= 0.15 * na + 12

def _img(path):
    """(mime, base64) da figura, reduzida para gastar pouco da cota; None se não der."""
    try:
        try:
            from PIL import Image
            im = Image.open(path); im.thumbnail((640, 640)); im = im.convert('RGB')
            buf = io.BytesIO(); im.save(buf, 'JPEG', quality=70)
            return 'image/jpeg', base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            b = open(path, 'rb').read()
            if len(b) > 400_000: return None
            return ('image/png' if path.lower().endswith('.png') else 'image/jpeg'), base64.b64encode(b).decode()
    except Exception:
        return None

def organiza_dia(blocos):
    """Recebe os blocos de um dia ([{k,e|src}]) e devolve a lista reorganizada, ou None (fica como está)."""
    if not ATIVA or len(blocos) < 3 or len(blocos) > 150: return None
    itens, imgs, tem_texto = [], {}, []
    for b in blocos:
        if b['k'] == 'img':
            iid = f'i{len(imgs) + 1}'; imgs[iid] = b['src']
            itens.append({'id': iid, 'kind': 'picture'})
        else:
            itens.append({'id': f't{len(itens) + 1}', 'kind': KIND.get(b['k'], 'paragraph'), 'text': b['e']}); tem_texto.append(b['e'])
    if not tem_texto: return None
    chave = hashlib.sha1(json.dumps([itens, [(s, os.path.getsize(s) if os.path.exists(s) else 0) for s in imgs.values()]],
                                    ensure_ascii=False).encode()).hexdigest()[:20]
    if chave in cache(): return cache()[chave] or None
    partes = [{'text': 'Items of this day (JSON):\n' + json.dumps(itens, ensure_ascii=False)}]
    if VE_IMAGENS:
        for iid, src in imgs.items():
            im = _img(src) if os.path.exists(src) else None
            if im:
                partes += [{'text': f'Picture {iid}:'}, {'inline_data': {'mime_type': im[0], 'data': im[1]}}]
    r = gera(partes, SIS_ORG if VE_IMAGENS else SIS_ORG_SEM_VISAO)
    if not isinstance(r, dict) or not isinstance(r.get('blocks'), list):
        return None                                      # sem resposta: não grava no cache (tenta de novo depois)
    saida, textos = [], []
    for b in r['blocks']:
        if not isinstance(b, dict): continue
        t = b.get('type')
        if t == 'image':
            src = imgs.get(str(b.get('image')))
            if src: saida.append({'k': 'img', 'src': src, 'e': str(b.get('caption') or '').strip()})
        else:
            txt = str(b.get('text') or '').strip()
            if txt:
                textos.append(txt)
                saida.append({'k': {'heading': 'h', 'list_item': 'li'}.get(t, 'p'), 'e': PREFIXO.get(t, '') + txt})
    if not cobre(' '.join(tem_texto), ' '.join(textos)):
        print('  IA: resposta descartada (mudou o conteúdo); mantido o texto extraído', file=sys.stderr)
        cache()[chave] = []                              # lembra que esta versão não serve
        return None
    cache()[chave] = saida
    return saida

def organiza_semana(w):
    """Organiza todos os dias de uma lição (modifica e devolve w)."""
    if not ATIVA: return w
    for d in w['days']:
        novo = organiza_dia(d['b'])
        if novo: d['b'] = novo
    salva_cache()
    return w
