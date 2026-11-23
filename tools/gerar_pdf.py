#!/usr/bin/env python3
"""Lições que só existem em PDF (Juniores 10–12 e Teens 13–14): acha os PDFs na página,
extrai texto e imagens, traduz para o português e grava no MESMO formato das outras lições.

Roda no GitHub Actions depois de gerar_infantil.py:
  python tools/gerar_pdf.py auto            # todos os níveis de tools/fontes_pdf.json
  python tools/gerar_pdf.py auto jr         # só um nível
  python tools/gerar_pdf.py arquivo.pdf jr  # um PDF local (teste)
Precisa de:  pip install pymupdf
"""
import collections, hashlib, html.parser, json, os, re, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gerar_infantil as G

try:
    import pymupdf as fitz
except ImportError:
    import fitz

CFG = os.environ.get('FONTES_PDF') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fontes_pdf.json')
DIAS_EN = r'(?:sunday|monday|tuesday|wednesday|thursday|friday|saturday|sabbath)'
RE_LICAO = re.compile(r'^\s*(?:lesson|session|week|lição|licao)\s*(\d{1,2})\b[\s:.\-–—]*(.*)$', re.I | re.S)
RE_DIA = re.compile(r'^\s*' + DIAS_EN + r'\b', re.I)
BULLETS = '•·▪●◦-–—*'
# PDFs com estas palavras no endereço ou no rótulo são do professor e são descartados
EXCLUIR_PADRAO = r'teacher|professor|(?:^|[-_/ ])tg(?:[-_. ]|$)|leader|powerpoint|\.pptx?\b|lesson[- ]?plan|guide for|kit\b'

# ------------------------------------------------------------------ achar os PDFs na página
class Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.out = []; self._h = None; self._t = []
    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == 'a' and a.get('href'):
            self._h, self._t = a['href'], []
        for k in ('src', 'data-src', 'data-href', 'data'):
            if tag in ('iframe', 'embed', 'object', 'div', 'button') and a.get(k):
                self.out.append((a[k], a.get('title', '')))
    def handle_data(self, d):
        if self._h is not None: self._t.append(d)
    def handle_endtag(self, tag):
        if tag == 'a' and self._h is not None:
            self.out.append((self._h, ' '.join(''.join(self._t).split()))); self._h = None

def normaliza(url):
    m = re.search(r'docs\.google\.com/(?:document|presentation)/d/([\w-]+)', url)
    if m: return f'https://docs.google.com/document/d/{m.group(1)}/export?format=pdf'
    m = re.search(r'drive\.google\.com/(?:file/d/|open\?id=)([\w-]+)', url)
    if m: return f'https://drive.google.com/uc?export=download&id={m.group(1)}'
    return url

def eh_pdf(url):
    u = url.lower()
    return '.pdf' in u or 'export?format=pdf' in u or 'drive.google.com' in u or 'docs.google.com/document' in u

def baixa_texto(url):
    b = G.http(url, raw=True)
    return b.decode('utf8', 'ignore') if b else ''

def achar_pdfs(pagina, profundidade=1):
    """Devolve [(url, rótulo)] de PDFs encontrados na página (e, se não achar nenhum, nas subpáginas)."""
    ach, subs = {}, []
    h = baixa_texto(pagina)
    p = Links(); p.feed(h)
    host = urllib.parse.urlparse(pagina).netloc
    for href, txt in p.out:
        u = urllib.parse.urljoin(pagina, href.strip())
        if eh_pdf(u): ach.setdefault(normaliza(u), txt)
        elif urllib.parse.urlparse(u).netloc == host and re.search(r'/page\d+', u) and u != pagina: subs.append(u)
    if not ach and profundidade > 0:
        for u in list(dict.fromkeys(subs))[:40]:
            for k, v in achar_pdfs(u, 0):
                ach.setdefault(k, v)
    return list(ach.items())

# ------------------------------------------------------------------ ler o PDF
def junta_linhas(linhas):
    s = ''
    for ln in linhas:
        ln = ln.strip()
        if not ln: continue
        if s.endswith('-') and ln[:1].islower(): s = s[:-1] + ln   # palavra quebrada no fim da linha
        else: s += (' ' if s else '') + ln
    return re.sub(r'\s+', ' ', s).strip()

def itens_do_pdf(dados):
    """Lê o PDF e devolve uma lista, na ordem de leitura, de itens {k:h|p|li|img, e|raw, ext, pag}."""
    doc = fitz.open(stream=dados, filetype='pdf')
    cont = collections.Counter(); paginas = []
    for page in doc:
        d = page.get_text('dict'); paginas.append((page, d))
        for b in d['blocks']:
            if b['type'] == 0:
                for l in b['lines']:
                    for s in l['spans']: cont[round(s['size'])] += len(s['text'].strip())
    corpo = cont.most_common(1)[0][0] if cont else 11
    # textos que se repetem em muitas páginas (cabeçalho/rodapé) e imagens repetidas (logotipos)
    rep, imgrep = collections.Counter(), collections.Counter()
    for page, d in paginas:
        vistos = set()
        for b in d['blocks']:
            if b['type'] == 0:
                t = re.sub(r'\d+', '#', junta_linhas(''.join(s['text'] for s in l['spans']) for l in b['lines'])).lower()
                if t and t not in vistos: rep[t] += 1; vistos.add(t)
            elif b['type'] == 1 and b.get('image'):
                imgrep[hashlib.md5(b['image']).hexdigest()] += 1
    out = []
    for pn, (page, d) in enumerate(paginas, 1):
        H, W = page.rect.height, page.rect.width
        for b in d['blocks']:
            if b['type'] == 1 and b.get('image'):
                x0, y0, x1, y1 = b['bbox']
                w, h = x1 - x0, y1 - y0
                if w < 70 or h < 70: continue                        # enfeite pequeno
                if w * h > 0.88 * W * H and len(page.get_text().strip()) > 40: continue  # fundo da página
                if imgrep[hashlib.md5(b['image']).hexdigest()] >= 3: continue            # logotipo repetido
                out.append({'k': 'img', 'raw': b['image'], 'ext': b.get('ext', 'png'), 'pag': pn})
            elif b['type'] == 0:
                linhas = [''.join(s['text'] for s in l['spans']) for l in b['lines']]
                texto = junta_linhas(linhas)
                if not texto: continue
                y0, y1 = b['bbox'][1], b['bbox'][3]
                chave = re.sub(r'\d+', '#', texto).lower()
                if re.fullmatch(r'[\d\s./|-]+', texto): continue                         # número de página
                if (y1 < H * 0.06 or y0 > H * 0.94) and len(texto) < 70: continue       # cabeçalho/rodapé
                if rep[chave] >= 3 and len(texto) < 90: continue
                spans = [s for l in b['lines'] for s in l['spans'] if s['text'].strip()]
                if not spans: continue
                tam = max(s['size'] for s in spans)
                neg = sum(len(s['text']) for s in spans if (s['flags'] & 16) or 'bold' in s['font'].lower()) / max(1, sum(len(s['text']) for s in spans))
                if texto[0] in BULLETS and len(texto) > 2 and texto[1] == ' ':
                    out.append({'k': 'li', 'e': texto[2:].strip(), 'pag': pn}); continue
                titulo = tam >= corpo * 1.25 or (neg > 0.8 and len(texto) < 90 and tam >= corpo)
                out.append({'k': 'h' if titulo else 'p', 'e': texto, 'pag': pn})
    return out

def monta_licoes(itens, n_padrao):
    """Divide a lista de itens em lições (por 'Lesson N') e dias (por Sunday…Sabbath ou por partes)."""
    licoes, L, D = [], None, None
    def nova_licao(n, titulo=''):
        nonlocal L, D
        L = {'n': n, 'title': titulo, 'days': []}; licoes.append(L); D = None
    def novo_dia(t=''):
        nonlocal D
        D = {'t': t, 'b': []}; L['days'].append(D)
    i = 0
    while i < len(itens):
        it = itens[i]; i += 1
        txt = it.get('e', '')
        if it['k'] in ('h', 'p') and len(txt) <= 90:
            m = RE_LICAO.match(txt)
            if m:
                titulo = m.group(2).strip()
                if not titulo and i < len(itens) and itens[i]['k'] == 'h' and len(itens[i].get('e', '')) < 120:
                    titulo = itens[i]['e']; i += 1
                nova_licao(int(m.group(1)), titulo); continue
            if RE_DIA.match(txt):
                if L is None: nova_licao(n_padrao)
                novo_dia(txt); continue
        if L is None: nova_licao(n_padrao)
        if D is None: novo_dia('')
        D['b'].append(it)
    # dias sem título ou muito longos viram "Parte 1, 2…"
    for L in licoes:
        dias = []
        for D in L['days']:
            if not D['b']: continue
            if len(D['b']) > 40:
                partes, atual = [], []
                for b in D['b']:
                    if atual and ((b['k'] == 'h' and len(atual) >= 12) or len(atual) >= 40): partes.append(atual); atual = []
                    atual.append(b)
                if atual: partes.append(atual)
                for j, pt in enumerate(partes, 1): dias.append({'t': (D['t'] or 'Parte') + (f' ({j})' if len(partes) > 1 else ''), 'b': pt})
            else: dias.append(D)
        for j, D in enumerate(dias, 1):
            if not D['t']: D['t'] = f'Parte {j}'
        L['days'] = dias
    return [L for L in licoes if L['days']]

# ------------------------------------------------------------------ gravar
def salva_imagens(L, rid):
    pasta = f'{G.IMG}/{rid}'; k = 0
    for D in L['days']:
        b2 = []
        for b in D['b']:
            if b['k'] == 'img':
                k += 1; ext = 'jpg' if b['ext'] in ('jpg', 'jpeg') else 'png'; raw = b['raw']
                if b['ext'] not in ('png', 'jpg', 'jpeg'):
                    try: raw = fitz.Pixmap(raw).tobytes('png')
                    except Exception: continue
                rel = f'{pasta}/{L["n"]:02d}-{k:02d}.{ext}'
                os.makedirs(pasta, exist_ok=True)
                if not os.path.exists(rel): open(rel, 'wb').write(raw)
                b2.append({'k': 'img', 'src': rel, 'e': ''})
            else: b2.append({'k': b['k'], 'e': b['e']})
        D['b'] = b2

def processa_nivel(lv, fontes, trimestre):
    rid = f'{trimestre}-{lv}'
    ano, tri = trimestre.split('-')
    subst = lambda s: s.replace('{ANO}', ano).replace('{AA}', ano[2:]).replace('{TRI}', str(int(tri)))
    explic, fixo = [(subst(u), '') for u in fontes.get('pdfs', [])], {}
    pad = fontes.get('padrao')
    if pad:   # endereço com {ANO} {AA} {TRI} {NN}: um PDF por lição (os que não existirem são ignorados)
        for n in range(int(fontes.get('de', 1)), int(fontes.get('ate', 13)) + 1):
            u = subst(pad).replace('{NN}', f'{n:02d}')
            explic.append((u, '')); fixo[u] = n
    lista = achar_pdfs(subst(fontes['pagina'])) if fontes.get('pagina') else []
    filtro = fontes.get('filtro')
    if filtro: lista = [(u, t) for u, t in lista if re.search(filtro, u + ' ' + t, re.I)]
    excl = fontes.get('excluir', EXCLUIR_PADRAO)   # por padrão, descarta material do PROFESSOR
    if excl:
        fora = [(u, t) for u, t in lista if re.search(excl, u + ' ' + t, re.I)]
        lista = [(u, t) for u, t in lista if (u, t) not in fora]
        for u, t in fora: print('   × ignorado (parece material do professor):', t or '', u)
    lista = explic + lista   # links informados à mão não passam pelo filtro de "professor"
    print(f'{rid}: {len(lista)} PDF(s) para tentar')
    for u, t in lista[:60]: print('   ·', t or '(sem rótulo)', '→', u)
    if len(lista) > 40: print('   AVISO: muitos PDFs; use "filtro" em tools/fontes_pdf.json para ficar só com o trimestre atual.')
    semanas = {}
    for ordem, (u, t) in enumerate(lista[:40], 1):
        dados = u if isinstance(u, bytes) else G.http(u, raw=True, tries=2)
        if not dados or dados[:4] != b'%PDF':
            print(f'   (não é PDF ou não baixou: {u})'); continue
        try:
            n_nome = re.findall(r'(\d{1,2})', os.path.basename(urllib.parse.urlparse(u).path))
            licoes = monta_licoes(itens_do_pdf(dados), int(n_nome[-1]) if n_nome and 1 <= int(n_nome[-1]) <= 14 else ordem)
        except Exception:
            import traceback; print(f'   ERRO lendo {u}:', file=sys.stderr); traceback.print_exc(); continue
        if u in fixo and licoes:   # um PDF = uma lição: junta tudo na lição do número do arquivo
            licoes = [{'n': fixo[u], 'title': next((L['title'] for L in licoes if L['title']), ''),
                       'days': [D for L in licoes for D in L['days']]}]
        print(f'   {u.split("/")[-1][:50]}: {len(licoes)} lição(ões)')
        for L in licoes:
            if 1 <= L['n'] <= 14:
                tam = sum(len(D['b']) for D in L['days'])
                if L['n'] not in semanas or tam > semanas[L['n']][0]: semanas[L['n']] = (tam, L)
    sems = []
    for n in sorted(semanas):
        L = semanas[n][1]
        salva_imagens(L, rid)
        w = {'n': n, 'title': L['title'], 'start': '', 'end': '', 'days': [{'t': D['t'], 'd': '', 'b': D['b']} for D in L['days']]}
        try:
            sems.append(G.traduz_semana(w)); print(f'{rid}: lição {n} ok ({sum(len(D["b"]) for D in w["days"])} blocos)')
        except G.Incompleta:
            print(f'{rid}: lição {n} com tradução incompleta (fica para a próxima execução)', file=sys.stderr)
        finally:
            json.dump(G.cache, open(G.CACHE_FILE, 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))
    if not sems:
        print(f'{rid}: nada para gravar'); return
    nome, idade, emoji = G.NIVEIS.get(lv, (lv.upper(), '', '📖'))
    doc = {'id': rid, 'q': trimestre, 'lv': lv, 'nivel': nome, 'idade': idade, 'emoji': emoji, 'modo': 'dias', 'weeks': sems,
           'gerado': G.datetime.datetime.now(G.datetime.timezone.utc).strftime('%Y-%m-%d')}
    json.dump(doc, open(f'{G.OUT}/{rid}.json', 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))
    atualiza_indice({'id': rid, 'q': trimestre, 'lv': lv, 'nivel': nome, 'idade': idade, 'emoji': emoji,
                     'semanas': len(sems), 'file': f'data/infantil/{rid}.json'})

def atualiza_indice(item):
    p = f'{G.OUT}/index.json'
    idx = json.load(open(p, encoding='utf8')) if os.path.exists(p) else {'items': []}
    itens = {i['id']: i for i in idx['items']}; itens[item['id']] = item
    ordem = sorted(itens.values(), key=lambda i: (i['q'], list(G.NIVEIS).index(i['lv']) if i['lv'] in G.NIVEIS else 9))
    json.dump({'items': ordem}, open(p, 'w', encoding='utf8'), ensure_ascii=False, indent=1)

def main():
    a = sys.argv[1:]
    os.makedirs(G.OUT, exist_ok=True)
    if a and a[0].lower().endswith('.pdf'):          # teste local: um PDF
        lv = a[1] if len(a) > 1 else 'jr'
        dados = open(a[0], 'rb').read()
        G.http = lambda url, **k: dados          # em vez de baixar, usa o arquivo local
        processa_nivel(lv, {'pdfs': ['https://local/' + os.path.basename(a[0])]}, G.trimestres(None)[0]); return
    cfg = json.load(open(CFG, encoding='utf8'))
    so = a[1].split(',') if len(a) > 1 else None
    print(f'tradutor: {G.TRAD}')
    for lv, f in cfg.items():
        if lv.startswith('_') or (so and lv not in so): continue
        if not f.get('ativo', True):
            print(f'{lv}: "ativo": false em tools/fontes_pdf.json (ignorado)'); continue
        try:
            processa_nivel(lv, f, f.get('trimestre') or G.trimestres(None)[0])
        except Exception:
            import traceback; print(f'{lv}: ERRO:', file=sys.stderr); traceback.print_exc()

if __name__ == '__main__':
    main()
