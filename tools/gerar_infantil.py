#!/usr/bin/env python3
"""Busca as lições infantojuvenis (Alive in Jesus) na API da Adventech, baixa as
imagens, traduz o texto do inglês para o português e grava data/infantil/*.json.

Roda no GitHub Actions (que tem acesso livre à internet) — veja .github/workflows/infantil.yml.

Uso:
  python tools/gerar_infantil.py                 # trimestre atual (e o próximo, se já existir)
  python tools/gerar_infantil.py 2026-04         # um trimestre específico
  python tools/gerar_infantil.py 2026-04 pr,kd   # só alguns níveis

Variáveis de ambiente (opcionais):
  ANTHROPIC_API_KEY   usa o Claude para traduzir (melhor, linguagem de criança). Sem ela usa o Google Tradutor gratuito.
  CLAUDE_MODEL        modelo do Claude (padrão: claude-haiku-5-5)
  API_BASE            raiz da API (padrão: https://sabbath-school.adventech.io/api/v3/en/aij)
  TRADUTOR            "google" | "claude" | "teste" (teste = não traduz de verdade; só para testar)
"""
import datetime, hashlib, json, os, re, sys, time, urllib.parse, urllib.request, urllib.error

API = os.environ.get('API_BASE', 'https://sabbath-school.adventech.io/api/v3/en/aij').rstrip('/')
OUT = 'data/infantil'
IMG = 'img/infantil'
CACHE_FILE = f'{OUT}/_traducao.json'
UA = {'User-Agent': 'Mozilla/5.0 (licao-infantil; uso na igreja)'}

# código usado pela Adventech no final do id → nome mostrado no app. Níveis que não existirem são ignorados.
NIVEIS = {
    'bg': ('Beginner', '1 a 3 anos', '🐣'),
    'kd': ('Jardim', '4 a 6 anos', '🌱'),
    'pr': ('Primários', '7 a 9 anos', '⭐'),
    'jr': ('Juniores', '10 a 12 anos', '🚀'),
}

# ---------------------------------------------------------------- rede
def http(url, data=None, headers=None, tries=3, raw=False):
    h = dict(UA); h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=45) as r:
                b = r.read()
                return b if raw else json.loads(b.decode('utf8'))
            wait = 2 * (i + 1)
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 410):
                return None
            err = e
            wait = int(os.environ.get('ESPERA_429', '20')) * (i + 1) if e.code == 429 else 2 * (i + 1)  # 429 = pedidos demais: espera mais
        except ValueError:  # resposta não é JSON (ex.: página HTML de "não existe")
            return None
        except Exception as e:
            err = e
            wait = 2 * (i + 1)
        if i < tries - 1:
            time.sleep(wait)
    print('  falhou:', url, err, file=sys.stderr)
    return None

# ---------------------------------------------------------------- limpeza do markdown da Adventech
def limpa(md):
    """Texto simples a partir do markdown da API (remove estilos ^[..]({..}), links e ênfases)."""
    if isinstance(md, dict):
        md = md.get('markdown') or md.get('text') or md.get('title') or ''
    if not isinstance(md, str):
        return ''
    s = md
    for _ in range(3):  # estilos aninhados
        s = re.sub(r'\^\[([^\]]*)\]\(\{[^)]*\}\)', r'\1', s)
    s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)
    s = re.sub(r'\[([^\]]*)\]\((?:sspm\w+|https?|mailto)[^)]*\)', r'\1', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'(\*\*|__)(.+?)\1', r'\2', s)
    s = re.sub(r'(?<![\w*])[*_](\S(?:.*?\S)?)[*_](?![\w*])', r'\1', s)
    s = re.sub(r'^\s{0,3}#{1,6}\s*', '', s)
    return re.sub(r'[ \t]+', ' ', s).strip()

def blocos(lista, nivel=0):
    """Converte os blocos da API em blocos simples: p, h, li, img (excertos bíblicos são ignorados)."""
    out = []
    if not isinstance(lista, list):
        return out
    for b in lista:
        if not isinstance(b, dict):
            continue
        t = b.get('type')
        if t == 'paragraph':
            x = limpa(b.get('markdown'))
            if x: out.append({'k': 'p', 'e': x})
        elif t == 'heading':
            x = limpa(b.get('markdown'))
            if x: out.append({'k': 'h', 'e': x})
        elif t == 'list':
            for it in b.get('items') or []:
                if not isinstance(it, dict):
                    continue
                x = limpa(it.get('markdown'))
                if x: out.append({'k': 'li', 'e': x})
                out += blocos(it.get('nested'), nivel + 1)
        elif t == 'image':
            if isinstance(b.get('src'), str) and b['src']:
                out.append({'k': 'img', 'src': b['src'], 'e': limpa(b.get('caption'))})
        elif t in ('question', 'quote', 'blockquote', 'callout'):
            x = limpa(b.get('markdown') or b.get('text'))
            if x: out.append({'k': 'p', 'e': x})
        # excerpt (texto bíblico em várias versões) e demais tipos: ignorados
    return out

# ---------------------------------------------------------------- tradução
TRAD = os.environ.get('TRADUTOR') or ('claude' if os.environ.get('ANTHROPIC_API_KEY') else 'google')
cache = {}
if os.path.exists(CACHE_FILE):
    cache = json.load(open(CACHE_FILE, encoding='utf8'))
chave = lambda s: hashlib.sha1(s.encode('utf8')).hexdigest()[:16]

SEP = '\n###\n'
falhas_google = 0  # pedidos seguidos que falharam; com 3 desiste nesta execução (continua na próxima)

def _g(q):
    data = urllib.parse.urlencode({'client': 'gtx', 'sl': 'en', 'tl': 'pt', 'dt': 't', 'q': q}).encode()
    j = http(os.environ.get('GOOGLE_URL', 'https://translate.googleapis.com/translate_a/single'), data=data,
             headers={'Content-Type': 'application/x-www-form-urlencoded'}, tries=3)
    time.sleep(float(os.environ.get('PAUSA_GOOGLE', '2')))
    if not j: return None
    return ''.join(seg[0] for seg in j[0] if seg and seg[0])

def t_google(textos):
    """Agrupa vários textos por pedido (muito menos chance de bloqueio 429)."""
    global falhas_google
    res = [None] * len(textos)
    i = 0
    while i < len(textos) and falhas_google < 3:
        lote, tam = [i], len(textos[i]); i += 1
        while i < len(textos) and len(lote) < 8 and tam + len(textos[i]) < 1500:
            lote.append(i); tam += len(textos[i]); i += 1
        out = _g(SEP.join(textos[k] for k in lote))
        if out is None:
            falhas_google += 1; continue
        falhas_google = 0
        partes = [p.strip() for p in out.split('###')]
        if len(partes) == len(lote):
            for k, p in zip(lote, partes): res[k] = p
        elif len(lote) == 1:
            res[lote[0]] = out.strip()
        else:  # o tradutor mexeu nos separadores: traduz um por um
            for k in lote:
                if falhas_google >= 3: break
                o = _g(textos[k])
                if o is None: falhas_google += 1
                else: falhas_google = 0; res[k] = o.strip()
    if falhas_google >= 3:
        print('  Google Tradutor está bloqueando pedidos; o que faltar será feito na próxima execução.', file=sys.stderr)
    return res

PROMPT = ('Você traduz lições da Escola Sabatina infantil (igreja adventista) do inglês para o português do Brasil. '
          'Use linguagem simples e carinhosa para crianças, mantenha os nomes bíblicos na forma usada na Bíblia em português '
          '(ex.: Genesis→Gênesis, Noah→Noé) e preserve números e perguntas. Responda SOMENTE com um array JSON de strings, '
          'na mesma ordem e quantidade do array recebido.')

def t_claude(textos):
    key = os.environ['ANTHROPIC_API_KEY']
    model = os.environ.get('CLAUDE_MODEL', 'claude-haiku-5-5')
    res = [None] * len(textos)
    lote, tam = [], 0
    def envia(idx):
        body = json.dumps({'model': model, 'max_tokens': 8000, 'system': PROMPT,
                           'messages': [{'role': 'user', 'content': json.dumps([textos[i] for i in idx], ensure_ascii=False)}]}).encode()
        j = http('https://api.anthropic.com/v1/messages', data=body, headers={
            'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
        try:
            txt = ''.join(c.get('text', '') for c in j['content'])
            arr = json.loads(re.search(r'\[.*\]', txt, re.S).group(0))
            if len(arr) == len(idx):
                for i, v in zip(idx, arr): res[i] = str(v)
        except Exception as e:
            print('  Claude: resposta inesperada', e, file=sys.stderr)
    for i, s in enumerate(textos):
        lote.append(i); tam += len(s)
        if tam > 6000 or len(lote) >= 40:
            envia(lote); lote, tam = [], 0
    if lote: envia(lote)
    return res

def traduz(textos):
    """Traduz uma lista de textos usando cache; devolve lista na mesma ordem."""
    falta = []
    for s in textos:
        if chave(s) not in cache and s not in falta: falta.append(s)
    if falta:
        if TRAD == 'teste':
            novos = ['[PT] ' + s for s in falta]
        elif TRAD == 'claude':
            novos = t_claude(falta)
            if any(v is None for v in novos):  # o que o Claude não devolveu, tenta no Google
                re_ = t_google([s for s, v in zip(falta, novos) if v is None]); it = iter(re_)
                novos = [v if v is not None else next(it) for v in novos]
        else:
            novos = t_google(falta)
        for s, v in zip(falta, novos):
            if v: cache[chave(s)] = v
    global FALTOU
    if any(chave(s) not in cache for s in textos): FALTOU = True
    return [cache.get(chave(s), s) for s in textos]  # sem tradução → mantém o inglês (a semana é pulada, veja traduz_semana)

FALTOU = False
class Incompleta(Exception): pass

# ---------------------------------------------------------------- semanas
def baixa_img(url, pasta, nome):
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or '.png'
    rel = f'{pasta}/{nome}{ext}'
    if os.path.exists(rel): return rel
    b = http(url, raw=True)
    if not b: return url  # não baixou: usa o link remoto
    os.makedirs(pasta, exist_ok=True)
    open(rel, 'wb').write(b)
    return rel

def semana(rid, n):
    j = http(f'{API}/{rid}/{n:02d}/index.json')
    if not j or not j.get('segments'): return None
    pasta = f'{IMG}/{rid}'
    dias, contador = [], 0
    for seg in j['segments']:
        if not isinstance(seg, dict):
            continue
        bl = blocos(seg.get('blocks'))
        if not bl: continue
        for b in bl:
            if b['k'] == 'img':
                contador += 1
                b['src'] = baixa_img(b['src'], pasta, f'{n:02d}-{contador:02d}')
        dias.append({'t': limpa(seg.get('title')), 'd': seg.get('date') or '', 'b': bl})
    if not dias: return None
    return {'n': n, 'title': limpa(j.get('title')), 'start': j.get('startDate') or '', 'end': j.get('endDate') or '', 'days': dias}

def traduz_semana(w):
    refs = []
    def add(d, campo='e'):
        if d.get(campo): refs.append(d)
    add(w, 'title')
    for d in w['days']:
        add(d, 't')
        for b in d['b']: add(b)
    # guarda o original em inglês e preenche o português
    orig = [(r.get('e') if 'e' in r else r.get('title') if r is w else r.get('t')) for r in refs]
    global FALTOU
    FALTOU = False
    pt = traduz(orig)
    if FALTOU:
        raise Incompleta()  # não salva semana com pedaços em inglês; o que já foi traduzido fica no cache
    for r, en, x in zip(refs, orig, pt):
        if r is w: r['title_en'], r['title'] = en, x
        elif 'k' in r: r['x'] = x           # bloco: e=inglês, x=português
        else: r['t_en'], r['t'] = en, x      # dia
    return w

def trimestres(arg):
    if arg: return [arg]
    h = datetime.date.today()
    q = (h.month - 1) // 3 + 1
    atual = f'{h.year}-0{q}'
    prox = f'{h.year + (q == 4)}-0{q % 4 + 1}'
    return [atual, prox]

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != 'auto' else None
    niveis = sys.argv[2].split(',') if len(sys.argv) > 2 else list(NIVEIS)
    os.makedirs(OUT, exist_ok=True)
    idx_path = f'{OUT}/index.json'
    idx = json.load(open(idx_path, encoding='utf8')) if os.path.exists(idx_path) else {'items': []}
    itens = {i['id']: i for i in idx['items']}
    print(f'tradutor: {TRAD} | API: {API}')
    for q in trimestres(arg):
        for lv in niveis:
            rid = f'{q}-{lv}'
            if not http(f'{API}/{rid}/01/index.json', tries=1) and not os.path.exists(f'{OUT}/{rid}.json'):
                print(f'{rid}: não encontrado (ignorado)'); continue
            sems = []
            for n in range(1, 15):
                try:
                    w = semana(rid, n)
                    if not w:
                        if n == 1: break
                        continue
                    print(f'{rid}: semana {n} ({sum(len(d["b"]) for d in w["days"])} blocos)')
                    sems.append(traduz_semana(w))
                except Incompleta:
                    print(f'{rid}: semana {n} com tradução incompleta (fica para a próxima execução)', file=sys.stderr)
                    json.dump(cache, open(CACHE_FILE, 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))
                    continue
                except Exception:
                    import traceback
                    print(f'{rid}: ERRO na semana {n} (pulada):', file=sys.stderr)
                    traceback.print_exc()
                    continue
                json.dump(cache, open(CACHE_FILE, 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))
            if not sems: continue
            nome, idade, emoji = NIVEIS.get(lv, (lv.upper(), '', '📖'))
            doc = {'id': rid, 'q': q, 'lv': lv, 'nivel': nome, 'idade': idade, 'emoji': emoji, 'weeks': sems,
                   'gerado': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}
            json.dump(doc, open(f'{OUT}/{rid}.json', 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))
            itens[rid] = {'id': rid, 'q': q, 'lv': lv, 'nivel': nome, 'idade': idade, 'emoji': emoji,
                          'semanas': len(sems), 'file': f'data/infantil/{rid}.json'}
    ordem = sorted(itens.values(), key=lambda i: (i['q'], list(NIVEIS).index(i['lv']) if i['lv'] in NIVEIS else 9))
    json.dump({'items': ordem}, open(idx_path, 'w', encoding='utf8'), ensure_ascii=False, indent=1)
    print('índice atualizado:', len(ordem), 'níveis/trimestres')

if __name__ == '__main__':
    main()
