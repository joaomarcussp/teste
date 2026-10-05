#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teamspack - transforma arquivos/pastas em mensagens de texto para o Teams e
restaura o original, byte a byte, a partir das mensagens recebidas.

Como fica pequeno:
  1. Compressão: testa várias estratégias (LZMA2 com vários parâmetros, filtro
     BCJ para executáveis, bzip2, deflate) e fica com a menor.
  2. Codificação: o modo padrão ("u") usa 32.768 ideogramas CJK/Hangul, ou seja
     15 bits por caractere - 2,5x mais denso que base64. O modo --ascii usa
     base64 (6 bits/caractere) para quando o caminho não aceitar Unicode.
  3. Divisão: cada parte respeita o limite de caracteres E de bytes UTF-8, tem
     CRC32 próprio e o pacote inteiro é conferido com SHA-256 ao restaurar.

Uso rápido:
  python teamspack.py enviar relatorio.pdf            # gera relatorio_teams/parte_*.txt
  python teamspack.py enviar minha_pasta --copiar     # copia parte por parte p/ colar no Teams
  python teamspack.py receber mensagens.txt           # restaura a partir do texto colado
  python teamspack.py receber --colar                 # captura as mensagens copiadas do Teams

Só usa a biblioteca padrão do Python (3.8+).
"""
from __future__ import annotations

import argparse
import base64
import bz2
import hashlib
import lzma
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor

VERSAO_FORMATO = 1
MAGIC = "TPK1"

LIMITE_CHARS_PADRAO = 33000
LIMITE_BYTES_PADRAO = 95000
LARGURA_ASCII_PADRAO = 100


class ErroTeamspack(Exception):
    """Erro com mensagem pronta para mostrar ao usuário."""


def _num(n):
    return f"{n:,}".replace(",", ".")


# ---------------------------------------------------------------------------
# Codificação bytes <-> texto
# ---------------------------------------------------------------------------

# 32.768 caracteres = 15 bits por caractere. Todos estão no plano básico (BMP:
# contam como 1 caractere no Teams/JavaScript e ocupam 3 bytes em UTF-8), são
# letras (categoria Lo, escrita da esquerda p/ direita), não são espaço,
# pontuação nem marcas combinantes, e existem desde o Unicode 3.0.
# Ideogramas CJK não mudam com nenhuma normalização Unicode; as sílabas Hangul
# podem ser decompostas (NFD/NFKD), por isso o decodificador aplica NFC antes.
_FAIXAS_U = ((0x4E00, 0x9FA5), (0x3400, 0x4DB5), (0xAC00, 0xC0A3))
ALFABETO_U = "".join(chr(c) for ini, fim in _FAIXAS_U for c in range(ini, fim + 1))
assert len(ALFABETO_U) == 1 << 15
_INDICE_U = {ch: i for i, ch in enumerate(ALFABETO_U)}
_CONJ_A = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")


class Modo:
    def __init__(self, letra, nome, bloco_bytes, bloco_chars, bytes_utf8, largura):
        self.letra = letra
        self.nome = nome
        self.bloco_bytes = bloco_bytes  # bytes de dados ...
        self.bloco_chars = bloco_chars  # ... viram exatamente estes caracteres
        self.bytes_utf8 = bytes_utf8  # bytes UTF-8 por caractere do payload
        self.largura = largura  # quebra de linha padrão (0 = sem quebra)


MODOS = {
    "u": Modo("u", "unicode, 15 bits/caractere", 15, 8, 3, 0),
    "a": Modo("a", "ascii/base64, 6 bits/caractere", 3, 4, 1, LARGURA_ASCII_PADRAO),
}


def codificar(modo, dados):
    if modo == "a":
        return base64.b64encode(dados).decode("ascii")
    if len(dados) % 15:
        raise ValueError("tamanho deve ser múltiplo de 15")
    a = ALFABETO_U
    m = 0x7FFF
    saida = []
    for i in range(0, len(dados), 15):
        n = int.from_bytes(dados[i:i + 15], "big")
        saida.append(a[n >> 105] + a[(n >> 90) & m] + a[(n >> 75) & m] + a[(n >> 60) & m]
                     + a[(n >> 45) & m] + a[(n >> 30) & m] + a[(n >> 15) & m] + a[n & m])
    return "".join(saida)


def decodificar(modo, texto):
    if modo == "a":
        return base64.b64decode(texto, validate=True)
    if len(texto) % 8:
        raise ValueError("tamanho deve ser múltiplo de 8")
    idx = _INDICE_U
    saida = bytearray()
    for i in range(0, len(texto), 8):
        n = 0
        for ch in texto[i:i + 8]:
            n = (n << 15) | idx[ch]
        saida += n.to_bytes(15, "big")
    return bytes(saida)


# ---------------------------------------------------------------------------
# Inteiros/strings compactos
# ---------------------------------------------------------------------------

def _uvarint(n):
    saida = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            saida.append(b | 0x80)
        else:
            saida.append(b)
            return bytes(saida)


def _str(s):
    b = s.encode("utf-8", "surrogateescape")
    return _uvarint(len(b)) + b


class _Leitor:
    def __init__(self, buf):
        self.buf = buf
        self.pos = 0

    def bytes(self, n):
        if n < 0 or self.pos + n > len(self.buf):
            raise ErroTeamspack("pacote truncado ou corrompido")
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def byte(self):
        return self.bytes(1)[0]

    def uvarint(self):
        n = desloc = 0
        while True:
            b = self.byte()
            n |= (b & 0x7F) << desloc
            if b < 0x80:
                return n
            desloc += 7
            if desloc > 63:
                raise ErroTeamspack("pacote corrompido (inteiro inválido)")

    def texto(self):
        return self.bytes(self.uvarint()).decode("utf-8", "surrogateescape")

    def resto(self):
        b = self.buf[self.pos:]
        self.pos = len(self.buf)
        return b


# ---------------------------------------------------------------------------
# Contêiner: o que vai dentro do pacote (antes da compressão)
# ---------------------------------------------------------------------------

TIPO_ARQUIVO, TIPO_VARIOS, TIPO_ZIP = 0, 1, 2


def _listar_entradas(entradas, excluir=None):
    """[(caminho relativo com '/', caminho no disco ou None para pasta vazia)]."""
    excluir = os.path.abspath(excluir) if excluir else None
    itens = []
    for entrada in entradas:
        absoluto = os.path.abspath(entrada)
        if os.path.isfile(absoluto):
            itens.append((os.path.basename(absoluto), absoluto))
        elif os.path.isdir(absoluto):
            base = os.path.dirname(absoluto)
            for raiz, pastas, arquivos in os.walk(absoluto):
                pastas[:] = sorted(p for p in pastas if os.path.join(raiz, p) != excluir)
                rel_raiz = os.path.relpath(raiz, base).replace(os.sep, "/")
                if not pastas and not arquivos:
                    itens.append((rel_raiz, None))
                for nome in sorted(arquivos):
                    itens.append((rel_raiz + "/" + nome, os.path.join(raiz, nome)))
        else:
            raise ErroTeamspack(f"não encontrado: {entrada}")
    vistos = set()
    for rel, _ in itens:
        if rel in vistos:
            raise ErroTeamspack(f"nome repetido entre as entradas: {rel}")
        vistos.add(rel)
    return itens


def _ler(caminho):
    with open(caminho, "rb") as f:
        return f.read()


def empacotar(entradas, expandir_zip=False, excluir=None):
    """Monta o contêiner. Devolve (bytes, descrição, tamanho original total)."""
    if expandir_zip:
        if len(entradas) != 1 or not os.path.isfile(entradas[0]):
            raise ErroTeamspack("--expandir-zip aceita exatamente um arquivo .zip")
        return _container_zip(entradas[0])

    if len(entradas) == 1 and os.path.isfile(entradas[0]):
        dados = _ler(entradas[0])
        nome = os.path.basename(os.path.abspath(entradas[0]))
        return (bytes([TIPO_ARQUIVO]) + _str(nome) + dados,
                f"{nome} ({_num(len(dados))} bytes)", len(dados))

    itens = _listar_entradas(entradas, excluir)

    # Agrupa arquivos parecidos (mesma extensão) para o compressor achar mais
    # repetições, como o 7-Zip faz. A ordem não importa na restauração.
    def chave(item):
        rel, disco = item
        nome = rel.rsplit("/", 1)[-1]
        ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
        return (disco is None, ext, nome, rel)

    itens.sort(key=chave)
    cab = bytearray([TIPO_VARIOS])
    cab += _uvarint(len(itens))
    corpos = []
    total = 0
    for rel, disco in itens:
        cab += _str(rel)
        if disco is None:
            cab += _uvarint(0)
        else:
            dados = _ler(disco)
            cab += _uvarint(len(dados) + 1)
            corpos.append(dados)
            total += len(dados)
    n_arq = sum(1 for _, d in itens if d is not None)
    return (bytes(cab) + b"".join(corpos),
            f"{n_arq} arquivo(s), {_num(total)} bytes", total)


def _container_zip(caminho):
    nome_zip = os.path.basename(os.path.abspath(caminho))
    try:
        zf = zipfile.ZipFile(caminho)
    except zipfile.BadZipFile:
        raise ErroTeamspack(f"{nome_zip} não é um .zip válido")
    with zf:
        infos = zf.infolist()
        cab = bytearray([TIPO_ZIP])
        cab += _str(nome_zip) + _uvarint(len(zf.comment)) + zf.comment + _uvarint(len(infos))
        corpos = []
        for zi in infos:
            if zi.flag_bits & 0x1:
                raise ErroTeamspack("zip com senha não pode ser expandido; envie sem --expandir-zip")
            try:
                dados = zf.read(zi)  # confere o CRC de cada membro
            except (NotImplementedError, zipfile.BadZipFile, RuntimeError) as e:
                raise ErroTeamspack(f"não consegui ler {zi.filename} dentro do zip ({e}); "
                                    "envie sem --expandir-zip")
            ano, mes, dia, hora, minuto, seg = zi.date_time
            dos = ((ano - 1980) << 25) | (mes << 21) | (dia << 16) | (hora << 11) | (minuto << 5) | (seg // 2)
            cab += _str(zi.filename) + dos.to_bytes(4, "big")
            cab += bytes([zi.compress_type & 0xFF, zi.create_system & 0xFF])
            cab += _uvarint(zi.external_attr) + _uvarint(len(zi.comment)) + zi.comment
            cab += _uvarint(len(dados))
            corpos.append(dados)
    total = sum(len(c) for c in corpos)
    return (bytes(cab) + b"".join(corpos),
            f"{nome_zip} expandido: {len(infos)} membro(s), {_num(total)} bytes descompactados",
            os.path.getsize(caminho))


def _caminho_seguro(destino, rel):
    rel_n = rel.replace("\\", "/")
    partes = [p for p in rel_n.split("/") if p not in ("", ".")]
    if (not partes or rel_n.startswith("/") or ".." in partes
            or re.match(r"^[A-Za-z]:", rel_n)):
        raise ErroTeamspack(f"caminho inseguro dentro do pacote: {rel!r}")
    return os.path.join(destino, *partes)


def ler_container(container):
    """Lista o conteúdo: (tipo, [(caminho relativo, dados ou None)], extras)."""
    r = _Leitor(container)
    tipo = r.byte()
    if tipo == TIPO_ARQUIVO:
        nome = r.texto()
        return tipo, [(nome, r.resto())], None
    if tipo == TIPO_VARIOS:
        n = r.uvarint()
        cab = [(r.texto(), r.uvarint()) for _ in range(n)]
        itens = [(rel, None if t == 0 else r.bytes(t - 1)) for rel, t in cab]
        return tipo, itens, None
    if tipo == TIPO_ZIP:
        nome_zip = r.texto()
        comentario = r.bytes(r.uvarint())
        membros = []
        for _ in range(r.uvarint()):
            nome = r.texto()
            dos = int.from_bytes(r.bytes(4), "big")
            data = ((dos >> 25) + 1980, (dos >> 21) & 0xF, (dos >> 16) & 0x1F,
                    (dos >> 11) & 0x1F, (dos >> 5) & 0x3F, (dos & 0x1F) * 2)
            metodo, sistema = r.byte(), r.byte()
            atrib = r.uvarint()
            com = r.bytes(r.uvarint())
            membros.append([nome, data, metodo, sistema, atrib, com, r.uvarint()])
        for m in membros:
            m[-1] = r.bytes(m[-1])
        return tipo, [(nome_zip, None)], (comentario, membros)
    raise ErroTeamspack("tipo de pacote desconhecido (versão mais nova do teamspack?)")


def extrair_container(container, destino, sobrescrever=False):
    """Grava o conteúdo em `destino`. Devolve a lista de caminhos criados."""
    tipo, itens, extras = ler_container(container)
    alvos = [(_caminho_seguro(destino, rel), dados) for rel, dados in itens]
    if not sobrescrever:
        existentes = [c for c, d in alvos if os.path.exists(c) and not (d is None and tipo != TIPO_ZIP and os.path.isdir(c))]
        if existentes:
            raise ErroTeamspack("já existe(m): " + ", ".join(existentes[:5])
                                + (" ..." if len(existentes) > 5 else "")
                                + "\nUse --sobrescrever ou escolha outro destino com -o.")
    criados = []
    if tipo == TIPO_ZIP:
        caminho = alvos[0][0]
        os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
        comentario, membros = extras
        with zipfile.ZipFile(caminho, "w") as zf:
            zf.comment = comentario
            for nome, data, metodo, sistema, atrib, com, dados in membros:
                zi = zipfile.ZipInfo(nome, data)
                zi.compress_type = metodo if metodo in (0, 8, 12, 14) else zipfile.ZIP_DEFLATED
                zi.create_system = sistema
                zi.external_attr = atrib
                zi.comment = com
                zf.writestr(zi, dados, compresslevel=9 if zi.compress_type in (8, 12) else None)
        return [caminho]
    for caminho, dados in alvos:
        if dados is None:
            os.makedirs(caminho, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
            with open(caminho, "wb") as f:
                f.write(dados)
        criados.append(caminho)
    return criados


# ---------------------------------------------------------------------------
# Compressão: testa várias e fica com a menor
# ---------------------------------------------------------------------------

MET_NENHUM, MET_LZMA, MET_BZ2, MET_DEFLATE = 0, 1, 2, 3
_PRE_NENHUM, _PRE_X86, _PRE_DELTA = 0, 1, 2
_DICT_MAX = 1 << 26  # 64 MiB, igual ao xz -9


def _filtros_lzma(pre, dist, dict_size, lc, lp, pb, extra=None):
    filtros = []
    if pre == _PRE_X86:
        filtros.append({"id": lzma.FILTER_X86})
    elif pre == _PRE_DELTA:
        filtros.append({"id": lzma.FILTER_DELTA, "dist": dist})
    lz = {"id": lzma.FILTER_LZMA2, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}
    if extra:
        lz.update(extra)
    filtros.append(lz)
    return filtros


def _lzma(dados, lc, lp, pb, pre=_PRE_NENHUM, dist=1, nice=273):
    dict_size = min(max(len(dados), 4096), _DICT_MAX)
    filtros = _filtros_lzma(pre, dist, dict_size, lc, lp, pb,
                            {"preset": 9 | lzma.PRESET_EXTREME, "nice_len": nice, "mf": lzma.MF_BT4})
    comp = lzma.compress(dados, format=lzma.FORMAT_RAW, filters=filtros)
    cab = bytes([MET_LZMA, (pb * 5 + lp) * 9 + lc, pre])
    if pre == _PRE_DELTA:
        cab += bytes([dist - 1])
    return cab + _uvarint(dict_size) + comp


def _parece_executavel(dados):
    return (b"\x7fELF" in dados[:65536] or b"This program cannot be run in DOS mode" in dados
            or b"\xcf\xfa\xed\xfe" in dados[:65536])


def _candidatos(dados, nivel):
    c = []
    if nivel == "rapido":
        return [("lzma2 lc3 lp0 pb2", lambda: _lzma(dados, 3, 0, 2))]
    props = [(3, 0, 2), (3, 0, 0), (4, 0, 0), (0, 2, 2)]
    if nivel == "exaustivo":
        props += [(2, 0, 0), (1, 0, 0), (0, 0, 0), (2, 2, 2), (0, 1, 1), (3, 1, 1)]
    for lc, lp, pb in props:
        c.append((f"lzma2 lc{lc} lp{lp} pb{pb}", lambda lc=lc, lp=lp, pb=pb: _lzma(dados, lc, lp, pb)))
        if nivel == "exaustivo":
            c.append((f"lzma2 lc{lc} lp{lp} pb{pb} nice64",
                      lambda lc=lc, lp=lp, pb=pb: _lzma(dados, lc, lp, pb, nice=64)))
    if nivel == "exaustivo" or _parece_executavel(dados):
        for lc, lp, pb in ((3, 0, 0), (4, 0, 0), (3, 0, 2), (0, 2, 2)):
            c.append((f"x86+lzma2 lc{lc} lp{lp} pb{pb}",
                      lambda lc=lc, lp=lp, pb=pb: _lzma(dados, lc, lp, pb, _PRE_X86)))
    if nivel == "exaustivo":
        for dist in (1, 2, 3, 4):
            for lc, lp, pb in ((0, 0, 0), (3, 0, 0)):
                c.append((f"delta{dist}+lzma2 lc{lc} lp{lp} pb{pb}",
                          lambda dist=dist, lc=lc, lp=lp, pb=pb: _lzma(dados, lc, lp, pb, _PRE_DELTA, dist)))
    c.append(("bzip2", lambda: bytes([MET_BZ2]) + bz2.compress(dados, 9)))
    c.append(("deflate", lambda: bytes([MET_DEFLATE]) + _deflate(dados)))
    c.append(("sem compressão", lambda: bytes([MET_NENHUM]) + dados))
    return c


def _deflate(dados):
    co = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
    return co.compress(dados) + co.flush()


def comprimir_melhor(dados, nivel="normal", log=None):
    """Devolve (bytes do método + dados comprimidos, nome do método vencedor)."""
    cands = _candidatos(dados, nivel)
    dict_size = min(max(len(dados), 4096), _DICT_MAX)
    # lzma/bz2/zlib liberam o GIL: threads rodam em paralelo de verdade.
    # Limita pela memória (o codificador LZMA usa ~12x o dicionário).
    por_mem = max(1, (1536 << 20) // (12 * dict_size + 2 * len(dados) + 1))
    trabalhadores = max(1, min(len(cands), os.cpu_count() or 1, por_mem))

    def rodar(cand):
        t0 = time.time()
        res = cand[1]()
        if log:
            log(f"    {cand[0]:<34} {_num(len(res)):>14} bytes  ({time.time() - t0:.1f} s)")
        return res

    with ThreadPoolExecutor(trabalhadores) as ex:
        resultados = list(ex.map(rodar, cands))
    i = min(range(len(cands)), key=lambda k: len(resultados[k]))  # empate: o primeiro
    return resultados[i], cands[i][0]


def descomprimir(buf):
    r = _Leitor(buf)
    metodo = r.byte()
    try:
        if metodo == MET_NENHUM:
            return r.resto()
        if metodo == MET_LZMA:
            props, pre = r.byte(), r.byte()
            dist = r.byte() + 1 if pre == _PRE_DELTA else 1
            dict_size = r.uvarint()
            lc, lp, pb = props % 9, (props // 9) % 5, props // 45
            filtros = _filtros_lzma(pre, dist, dict_size, lc, lp, pb)
            return lzma.decompress(r.resto(), format=lzma.FORMAT_RAW, filters=filtros)
        if metodo == MET_BZ2:
            return bz2.decompress(r.resto())
        if metodo == MET_DEFLATE:
            return zlib.decompress(r.resto(), -15)
    except (lzma.LZMAError, OSError, EOFError, zlib.error, ValueError) as e:
        raise ErroTeamspack(f"falha ao descomprimir: {e}")
    raise ErroTeamspack("método de compressão desconhecido (versão mais nova do teamspack?)")


# ---------------------------------------------------------------------------
# Pacote binário ("blob") e divisão em partes de texto
# ---------------------------------------------------------------------------

def montar_blob(container, comprimido, modo):
    """blob = tamanho + [versão, sha256[:8] do contêiner, método, dados] + enchimento."""
    nucleo = bytes([VERSAO_FORMATO]) + hashlib.sha256(container).digest()[:8] + comprimido
    blob = _uvarint(len(nucleo)) + nucleo
    return blob + b"\0" * (-len(blob) % MODOS[modo].bloco_bytes)


def abrir_blob(blob):
    r = _Leitor(blob)
    nucleo = _Leitor(r.bytes(r.uvarint()))
    versao = nucleo.byte()
    if versao != VERSAO_FORMATO:
        raise ErroTeamspack(f"pacote no formato v{versao}; este teamspack lê v{VERSAO_FORMATO}")
    sha = nucleo.bytes(8)
    container = descomprimir(nucleo.resto())
    if hashlib.sha256(container).digest()[:8] != sha:
        raise ErroTeamspack("verificação SHA-256 falhou: o conteúdo restaurado não confere")
    return container


def _cabecalho(modo, pid, i, n, crc, w):
    return f"{MAGIC}-{modo}-{pid}-{i:0{w}d}-{n:0{w}d}-{crc:08x}"


_FIM = MAGIC + "-FIM"


def _quebrar(texto, largura):
    if not largura:
        return texto
    return "\n".join(texto[k:k + largura] for k in range(0, len(texto), largura))


def _bytes_por_parte(modo, w, max_chars, max_bytes, largura):
    md = MODOS[modo]
    fixo = len(_cabecalho(modo, "0" * 6, 0, 0, 0, w)) + 1 + 1 + len(_FIM)  # 2 quebras de linha

    def cabe(p):
        nl = (p - 1) // largura if (largura and p) else 0
        return fixo + p + nl <= max_chars and fixo + p * md.bytes_utf8 + nl <= max_bytes

    p = (max_chars // md.bloco_chars) * md.bloco_chars
    while p > 0 and not cabe(p):
        p -= md.bloco_chars
    if p <= 0:
        raise ErroTeamspack("limites pequenos demais para caber uma parte")
    return p // md.bloco_chars * md.bloco_bytes


def planejar(tamanho_blob, modo, max_chars=LIMITE_CHARS_PADRAO, max_bytes=LIMITE_BYTES_PADRAO, largura=None):
    """Devolve (número de partes, largura dos números, bytes por parte)."""
    md = MODOS[modo]
    if largura is None:
        largura = md.largura
    w = 3
    while True:
        cap = _bytes_por_parte(modo, w, max_chars, max_bytes, largura)
        n = max(1, -(-tamanho_blob // cap))
        if len(str(n)) <= w:
            break
        w = len(str(n))
    # Distribui por igual: mesmo número de mensagens, todas um pouco menores.
    blocos = -(-tamanho_blob // md.bloco_bytes)
    return n, w, -(-blocos // n) * md.bloco_bytes


def gerar_partes(blob, modo, max_chars=LIMITE_CHARS_PADRAO, max_bytes=LIMITE_BYTES_PADRAO, largura=None):
    if largura is None:
        largura = MODOS[modo].largura
    n, w, por_parte = planejar(len(blob), modo, max_chars, max_bytes, largura)
    pid = hashlib.sha256(blob).hexdigest()[:6]
    partes = []
    for i in range(n):
        pedaco = blob[i * por_parte:(i + 1) * por_parte]
        txt = (_cabecalho(modo, pid, i + 1, n, zlib.crc32(pedaco), w) + "\n"
               + _quebrar(codificar(modo, pedaco), largura) + "\n" + _FIM)
        if len(txt) > max_chars or len(txt.encode("utf-8")) > max_bytes:
            raise AssertionError("parte excedeu o limite (bug)")
        partes.append(txt)
    return partes


# ---------------------------------------------------------------------------
# Leitura das partes (tolerante a lixo, espaços, ordem trocada, duplicatas)
# ---------------------------------------------------------------------------

_T = "[-‐‑‒–—―−]"  # aceita hífens "trocados"
_RE_CAB = re.compile(MAGIC + _T + "([ua])" + _T + "([0-9a-f]{6})" + _T + r"(\d{1,7})"
                     + _T + r"(\d{1,7})" + _T + "([0-9a-fA-F]{8})")
_RE_FIM = re.compile(MAGIC + _T + "FIM")
_INVISIVEIS = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)


def _faixas(nums):
    nums = sorted(nums)
    saida = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        saida.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(saida)


class Conjunto:
    def __init__(self, pid, modo, total):
        self.pid = pid
        self.modo = modo
        self.total = total
        self.partes = {}

    def faltando(self):
        return [i for i in range(1, self.total + 1) if i not in self.partes]

    def completo(self):
        return len(self.partes) == self.total

    def blob(self):
        return b"".join(self.partes[i] for i in range(1, self.total + 1))


class Coletor:
    """Junta partes vindas de vários textos/colagens."""

    def __init__(self):
        self.conjuntos = {}

    def adicionar(self, texto):
        """Processa um texto. Devolve (novas [(conjunto, i)], avisos [str])."""
        texto = unicodedata.normalize("NFC", texto)
        cabs = list(_RE_CAB.finditer(texto))
        novas, avisos = [], []
        for k, m in enumerate(cabs):
            modo, pid = m.group(1), m.group(2)
            i, n, crc = int(m.group(3)), int(m.group(4)), int(m.group(5), 16)
            rotulo = f"parte {i}/{n} [{pid}]"
            if not 1 <= i <= n:
                avisos.append(f"{rotulo}: numeração inválida")
                continue
            fim_seg = cabs[k + 1].start() if k + 1 < len(cabs) else len(texto)
            fim = _RE_FIM.search(texto, m.end(), fim_seg)
            if not fim:
                avisos.append(f"{rotulo}: incompleta (sem '{_FIM}' no final). Copie a mensagem inteira "
                              "(se o Teams mostrar 'Ver mais', expanda antes).")
                continue
            payload = "".join(texto[m.end():fim.start()].translate(_INVISIVEIS).split())
            md = MODOS[modo]
            if modo == "u":
                ruins = [c for c in payload if c not in _INDICE_U]
            else:
                ruins = [c for c in payload if c not in _CONJ_A]
            if ruins:
                dica = ""
                if "?" in ruins or "�" in ruins:
                    dica = " Parece um .txt salvo sem UTF-8: salve como UTF-8."
                avisos.append(f"{rotulo}: {len(ruins)} caractere(s) estranho(s), ex. {ruins[0]!r}; "
                              f"a mensagem foi alterada no caminho.{dica}")
                continue
            if len(payload) % md.bloco_chars:
                avisos.append(f"{rotulo}: tamanho inválido ({len(payload)} caracteres); mensagem cortada?")
                continue
            dados = decodificar(modo, payload)
            if zlib.crc32(dados) != crc:
                avisos.append(f"{rotulo}: CRC não confere (conteúdo alterado ou cortado)")
                continue
            conj = self.conjuntos.get(pid)
            if conj is None:
                conj = self.conjuntos[pid] = Conjunto(pid, modo, n)
            elif conj.total != n or conj.modo != modo:
                avisos.append(f"{rotulo}: não combina com as outras partes do conjunto {pid}")
                continue
            if i not in conj.partes:
                conj.partes[i] = dados
                novas.append((conj, i))
        return novas, avisos

    def completos(self):
        return [c for c in self.conjuntos.values() if c.completo()]


# ---------------------------------------------------------------------------
# Área de transferência (Windows via ctypes; macOS/Linux via comandos; Tk)
# ---------------------------------------------------------------------------

def _clip_windows():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002

    def abrir():
        for _ in range(100):  # outro programa pode estar com ela aberta
            if user32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise ErroTeamspack("não consegui abrir a área de transferência do Windows")

    def copiar(texto):
        dados = texto.encode("utf-16-le") + b"\0\0"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dados))
        if not h:
            raise ErroTeamspack("sem memória para a área de transferência")
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, dados, len(dados))
        kernel32.GlobalUnlock(h)
        abrir()
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                kernel32.GlobalFree(h)
                raise ErroTeamspack("falha ao copiar para a área de transferência")
        finally:
            user32.CloseClipboard()

    def colar():
        abrir()
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    return copiar, colar


def _clip_comandos(cmd_copiar, cmd_colar, env=None):
    def copiar(texto):
        subprocess.run(cmd_copiar, input=texto.encode("utf-8"), env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def colar():
        r = subprocess.run(cmd_colar, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""

    return copiar, colar


def _clip_tk():
    import tkinter

    raiz = tkinter.Tk()
    raiz.withdraw()

    def copiar(texto):
        raiz.clipboard_clear()
        raiz.clipboard_append(texto)
        raiz.update()

    def colar():
        try:
            raiz.update()
            return raiz.clipboard_get()
        except tkinter.TclError:
            return ""

    return copiar, colar


class AreaTransferencia:
    def __init__(self):
        if sys.platform == "win32":
            self.copiar, self.colar = _clip_windows()
            return
        if sys.platform == "darwin" and shutil.which("pbcopy"):
            env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
            self.copiar, self.colar = _clip_comandos(["pbcopy"], ["pbpaste"], env)
            return
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            self.copiar, self.colar = _clip_comandos(["wl-copy"], ["wl-paste", "--no-newline"])
            return
        if shutil.which("xclip"):
            self.copiar, self.colar = _clip_comandos(["xclip", "-selection", "clipboard"],
                                                     ["xclip", "-selection", "clipboard", "-o"])
            return
        if shutil.which("xsel"):
            self.copiar, self.colar = _clip_comandos(["xsel", "--clipboard", "--input"],
                                                     ["xsel", "--clipboard", "--output"])
            return
        try:
            self.copiar, self.colar = _clip_tk()
        except Exception:
            raise ErroTeamspack("não encontrei como acessar a área de transferência "
                                "(instale xclip/xsel/wl-clipboard ou use os arquivos .txt)")


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def _ler_texto(caminho):
    if caminho == "-":
        return sys.stdin.read()
    b = _ler(caminho)
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return b.decode("utf-16")
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def _arquivos_de(entradas, padrao=".txt"):
    arquivos = []
    for e in entradas:
        if os.path.isdir(e):
            arquivos += sorted(os.path.join(e, n) for n in os.listdir(e)
                               if n.lower().endswith(padrao) and os.path.isfile(os.path.join(e, n)))
        elif e == "-" or os.path.isfile(e):
            arquivos.append(e)
        else:
            raise ErroTeamspack(f"não encontrado: {e}")
    return arquivos


def _numeros(spec):
    nums = set()
    for pedaco in spec.replace(" ", "").split(","):
        if not pedaco:
            continue
        if "-" in pedaco:
            a, b = pedaco.split("-", 1)
            nums.update(range(int(a), int(b) + 1))
        else:
            nums.add(int(pedaco))
    return nums


def copiar_partes(partes, log=print, perguntar=input, area=None):
    """partes: [(i, n, texto)]. Copia uma por vez, esperando Enter entre elas."""
    area = area or AreaTransferencia()
    for k, (i, n, texto) in enumerate(partes, 1):
        area.copiar(texto)
        msg = (f"[{k}/{len(partes)}] parte {i}/{n} copiada ({_num(len(texto))} caracteres). "
               "Cole no Teams (Ctrl+V), envie")
        try:
            if k < len(partes):
                perguntar(msg + " e tecle Enter aqui para a próxima... ")
            else:
                log(msg + ". Era a última!")
        except (EOFError, KeyboardInterrupt):
            log("\nInterrompido.")
            return False
    return True


def cmd_enviar(args):
    log = (lambda *a, **k: None) if args.quieto else print
    modo = "a" if args.ascii else "u"
    md = MODOS[modo]
    largura = md.largura if args.largura is None else args.largura

    if args.saida:
        pasta = args.saida
    else:
        base = os.path.basename(os.path.abspath(args.entradas[0]))
        if len(args.entradas) == 1 and os.path.isfile(args.entradas[0]):
            base = os.path.splitext(base)[0] or base
        elif len(args.entradas) > 1:
            base = "pacote"
        pasta = base + "_teams"

    t0 = time.time()
    container, descricao, original = empacotar(args.entradas, args.expandir_zip, excluir=pasta)
    log(f"Entrada:      {descricao}")
    nivel = "rapido" if args.rapido else "exaustivo" if args.exaustivo else "normal"
    if len(container) > (8 << 20) and nivel != "rapido":
        log(f"              (testando compressões em {_num(len(container))} bytes; pode demorar)")
    comprimido, metodo = comprimir_melhor(container, nivel, log if args.verbose else None)
    blob = montar_blob(container, comprimido, modo)
    log(f"Compressão:   {metodo} -> {_num(len(blob))} bytes "
        f"({100 * len(blob) / max(1, original):.1f}% do original)  [{time.time() - t0:.1f} s]")

    partes = gerar_partes(blob, modo, args.max_chars, args.max_bytes, largura)
    maior = max(partes, key=len)
    log(f"Codificação:  {md.nome}")
    log(f"Partes:       {len(partes)} mensagem(ns); a maior tem {_num(len(maior))} caracteres / "
        f"{_num(len(maior.encode('utf-8')))} bytes UTF-8")
    outro = "a" if modo == "u" else "u"
    n_outro = planejar(len(montar_blob(container, comprimido, outro)), outro,
                       args.max_chars, args.max_bytes)[0]
    log(f"              (no modo {'--ascii' if outro == 'a' else 'unicode (padrão)'} seriam {n_outro})")

    # Confere a ida e volta antes de entregar qualquer coisa.
    col = Coletor()
    col.adicionar("\n\n".join(partes))
    if len(col.completos()) != 1 or abrir_blob(col.completos()[0].blob()) != container:
        raise AssertionError("verificação de ida e volta falhou (bug)")
    log("Verificação:  OK - as partes restauram o original byte a byte")

    os.makedirs(pasta, exist_ok=True)
    for antigo in os.listdir(pasta):  # remove partes de uma execução anterior
        if re.fullmatch(r"parte_\d+de\d+\.txt", antigo):
            os.remove(os.path.join(pasta, antigo))
    w = len(str(len(partes)))
    nomes = []
    for i, texto in enumerate(partes, 1):
        nome = os.path.join(pasta, f"parte_{i:0{w}d}de{len(partes)}.txt")
        with open(nome, "w", encoding="utf-8", newline="\n") as f:
            f.write(texto)
        nomes.append(nome)
    log(f"Arquivos:     {nomes[0]}" + (f" ... {os.path.basename(nomes[-1])}" if len(nomes) > 1 else ""))
    log("")
    log("Envie cada parte como uma mensagem no Teams. Quem recebe roda:")
    log("  python teamspack.py receber --colar          (e copia as mensagens no Teams)")
    log("  python teamspack.py receber mensagens.txt    (texto das mensagens colado num .txt)")

    if args.copiar:
        log("")
        copiar_partes([(i, len(partes), t) for i, t in enumerate(partes, 1)], log)
    return 0


def cmd_copiar(args):
    partes = []
    for arq in _arquivos_de(args.entradas):
        texto = _ler_texto(arq)
        cabs = list(_RE_CAB.finditer(texto))
        for k, m in enumerate(cabs):
            fim = _RE_FIM.search(texto, m.end(), cabs[k + 1].start() if k + 1 < len(cabs) else len(texto))
            if fim:
                partes.append((int(m.group(3)), int(m.group(4)), texto[m.start():fim.end()]))
    if args.partes:
        quero = _numeros(args.partes)
        partes = [p for p in partes if p[0] in quero]
    partes.sort(key=lambda p: p[0])
    if not partes:
        raise ErroTeamspack("nenhuma parte encontrada")
    return 0 if copiar_partes(partes) else 1


def _restaurar_completos(coletor, destino, sobrescrever, log):
    ok = True
    for conj in coletor.completos():
        try:
            criados = extrair_container(abrir_blob(conj.blob()), destino, sobrescrever)
        except ErroTeamspack as e:
            log(f"Conjunto {conj.pid}: ERRO - {e}")
            ok = False
            continue
        log(f"Conjunto {conj.pid}: {conj.total} parte(s) OK, SHA-256 conferido. Restaurado:")
        for c in criados[:20]:
            log(f"  {c}")
        if len(criados) > 20:
            log(f"  ... e mais {len(criados) - 20}")
    return ok


def _status(coletor):
    linhas = []
    for c in coletor.conjuntos.values():
        falta = c.faltando()
        linhas.append(f"[{c.pid}] {len(c.partes)}/{c.total} partes"
                      + (f" - faltam: {_faixas(falta)}" if falta else " - completo"))
    return linhas


def monitorar(area, coletor, log=print, intervalo=0.4, dormir=time.sleep):
    log("Monitorando a área de transferência. No Teams, selecione o texto de cada mensagem")
    log("e copie (Ctrl+C), em qualquer ordem. Ctrl+C aqui encerra.\n")
    anterior = None
    try:
        while True:
            texto = area.colar()
            if texto and texto != anterior:
                anterior = texto
                novas, avisos = coletor.adicionar(texto)
                for a in avisos:
                    log("  AVISO: " + a)
                if novas:
                    log("  + " + ", ".join(f"parte {i}/{c.total}" for c, i in novas)
                        + "  ->  " + " | ".join(_status(coletor)))
                elif not avisos and _RE_CAB.search(texto):
                    log("  (parte já recebida)")
            if coletor.completos():
                return True
            dormir(intervalo)
    except KeyboardInterrupt:
        log("\nInterrompido.")
        return False


def cmd_receber(args):
    log = print
    coletor = Coletor()
    if args.colar:
        monitorar(AreaTransferencia(), coletor, log)
    else:
        entradas = args.entradas
        if not entradas:
            if sys.stdin.isatty():
                raise ErroTeamspack("informe o(s) .txt com as mensagens, use '-' para ler da entrada "
                                    "padrão ou --colar para capturar da área de transferência")
            entradas = ["-"]
        for arq in _arquivos_de(entradas):
            _, avisos = coletor.adicionar(_ler_texto(arq))
            for a in avisos:
                log(f"AVISO ({arq}): {a}")
    if not coletor.conjuntos:
        raise ErroTeamspack(f"nenhuma parte válida encontrada (procurei por '{MAGIC}-...')")
    ok = _restaurar_completos(coletor, args.destino, args.sobrescrever, log)
    incompletos = [c for c in coletor.conjuntos.values() if not c.completo()]
    for c in incompletos:
        log(f"Conjunto {c.pid}: INCOMPLETO - tenho {len(c.partes)}/{c.total}; faltam as partes "
            f"{_faixas(c.faltando())}. No envio: python teamspack.py copiar <pasta_teams> "
            f"--partes {_faixas(c.faltando()).replace(' ', '')}")
    return 0 if ok and not incompletos else 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(
        prog="teamspack",
        description="Transforma arquivos em mensagens de texto para o Teams (e de volta).")
    sub = p.add_subparsers(dest="comando")
    sub.required = True

    e = sub.add_parser("enviar", help="arquivo(s)/pasta(s) -> partes de texto")
    e.add_argument("entradas", nargs="+", help="arquivos e/ou pastas")
    e.add_argument("-o", "--saida", help="pasta das partes (padrão: <nome>_teams)")
    e.add_argument("--ascii", action="store_true",
                   help="usa base64 (6 bits/caractere) em vez de Unicode (15 bits/caractere)")
    e.add_argument("--max-chars", type=int, default=LIMITE_CHARS_PADRAO,
                   help=f"máximo de caracteres por mensagem (padrão {LIMITE_CHARS_PADRAO})")
    e.add_argument("--max-bytes", type=int, default=LIMITE_BYTES_PADRAO,
                   help=f"máximo de bytes UTF-8 por mensagem (padrão {LIMITE_BYTES_PADRAO})")
    e.add_argument("--largura", type=int,
                   help="quebra o texto a cada N caracteres (padrão: 100 no --ascii, 0 no unicode)")
    e.add_argument("--expandir-zip", action="store_true",
                   help="descompacta o .zip/.docx/.xlsx e recomprime melhor; o zip recriado tem o "
                        "mesmo conteúdo, mas não é idêntico byte a byte")
    g = e.add_mutually_exclusive_group()
    g.add_argument("--rapido", action="store_true", help="só um método de compressão")
    g.add_argument("--exaustivo", action="store_true", help="testa muito mais combinações (lento)")
    e.add_argument("--copiar", action="store_true",
                   help="depois de gerar, copia parte por parte para a área de transferência")
    e.add_argument("-v", "--verbose", action="store_true", help="mostra o resultado de cada método")
    e.add_argument("-q", "--quieto", action="store_true")
    e.set_defaults(func=cmd_enviar)

    r = sub.add_parser("receber", help="mensagens de texto -> arquivo(s) original(is)")
    r.add_argument("entradas", nargs="*", help=".txt com as mensagens (ou pastas com .txt; '-' = stdin)")
    r.add_argument("-o", "--destino", default=".", help="onde gravar (padrão: pasta atual)")
    r.add_argument("--colar", action="store_true",
                   help="captura as mensagens conforme você as copia no Teams")
    r.add_argument("--sobrescrever", action="store_true")
    r.set_defaults(func=cmd_receber)

    c = sub.add_parser("copiar", help="copia partes já geradas, uma por vez")
    c.add_argument("entradas", nargs="+", help="pasta <nome>_teams ou arquivos de parte")
    c.add_argument("--partes", help="só estas partes, ex.: 3,7-9")
    c.set_defaults(func=cmd_copiar)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ErroTeamspack as ex:
        print(f"Erro: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
