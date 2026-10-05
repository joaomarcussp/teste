# -*- coding: utf-8 -*-
"""Testes do teamspack. Rode com:  python -m unittest test_teamspack -v"""
import io
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zipfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import teamspack as tp  # noqa: E402

AQUI = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(AQUI, "teamspack.py")


def _partes(entradas, modo="u", max_chars=tp.LIMITE_CHARS_PADRAO, max_bytes=tp.LIMITE_BYTES_PADRAO,
            nivel="rapido", expandir_zip=False):
    container, _, _ = tp.empacotar(entradas, expandir_zip)
    comp, _ = tp.comprimir_melhor(container, nivel)
    return container, tp.gerar_partes(tp.montar_blob(container, comp, modo), modo, max_chars, max_bytes)


def _restaurar(texto):
    col = tp.Coletor()
    _, avisos = col.adicionar(texto)
    completos = col.completos()
    return col, avisos, (tp.abrir_blob(completos[0].blob()) if completos else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rng = random.Random(1234)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def arquivo(self, nome, dados):
        caminho = os.path.join(self.tmp, nome)
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        with open(caminho, "wb") as f:
            f.write(dados)
        return caminho

    def texto_aleatorio(self, n):
        palavras = [b"def", b"return", b"self", b"dados", b"import", b"valor", b"(", b")", b"\n", b"    "]
        return b" ".join(self.rng.choice(palavras) for _ in range(n))


class TestAlfabeto(unittest.TestCase):
    def test_propriedades(self):
        a = tp.ALFABETO_U
        self.assertEqual(len(set(a)), 32768)
        for c in a:
            self.assertEqual(unicodedata.category(c), "Lo")
            self.assertEqual(unicodedata.bidirectional(c), "L")
            self.assertLessEqual(ord(c), 0xFFFF)
            self.assertEqual(unicodedata.normalize("NFC", c), c)
            for forma in ("NFD", "NFKC", "NFKD"):
                self.assertEqual(unicodedata.normalize("NFC", unicodedata.normalize(forma, c)), c)

    def test_codificacao(self):
        rng = random.Random(1)
        for n in (0, 15, 30, 15 * 77):
            d = bytes(rng.randrange(256) for _ in range(n))
            t = tp.codificar("u", d)
            self.assertEqual(len(t), n // 15 * 8)
            self.assertEqual(tp.decodificar("u", t), d)
        d = bytes(range(256)) * 3
        self.assertEqual(tp.decodificar("a", tp.codificar("a", d)), d)


class TestIdaEVolta(Base):
    def test_tamanhos_e_modos(self):
        for tamanho in (0, 1, 14, 15, 16, 1000, 60000):
            for modo in ("u", "a"):
                with self.subTest(tamanho=tamanho, modo=modo):
                    dados = bytes(self.rng.randrange(256) for _ in range(tamanho))
                    cam = self.arquivo(f"x{tamanho}.bin", dados)
                    container, partes = _partes([cam], modo, max_chars=4000, max_bytes=9000)
                    for p in partes:
                        self.assertLessEqual(len(p), 4000)
                        self.assertLessEqual(len(p.encode("utf-8")), 9000)
                    _, avisos, rest = _restaurar("\n".join(partes))
                    self.assertEqual(avisos, [])
                    self.assertEqual(rest, container)
                    _, itens, _ = tp.ler_container(rest)
                    self.assertEqual(itens, [(f"x{tamanho}.bin", dados)])

    def test_limites_padrao(self):
        dados = os.urandom(400000)
        cam = self.arquivo("grande.bin", dados)
        for modo, esperado_por_parte in (("u", 59000), ("a", 24000)):
            _, partes = _partes([cam], modo)
            for p in partes:
                self.assertLessEqual(len(p), tp.LIMITE_CHARS_PADRAO)
                self.assertLessEqual(len(p.encode("utf-8")), tp.LIMITE_BYTES_PADRAO)
            self.assertLessEqual(len(partes), -(-400100 // esperado_por_parte))

    def test_unicode_rende_mais_que_ascii(self):
        n_u = tp.planejar(3_000_000, "u")[0]
        n_a = tp.planejar(3_000_000, "a")[0]
        self.assertGreaterEqual(n_a / n_u, 2.35)

    def test_ruido_do_teams(self):
        cam = self.arquivo("t.txt", self.texto_aleatorio(30000))
        for modo in ("u", "a"):
            container, partes = _partes([cam], modo, max_chars=3000, max_bytes=8000)
            self.assertGreater(len(partes), 3)
            mexidas = []
            for p in partes:
                linhas = p.split("\n")
                corpo = "\r\n\r\n".join(linhas)  # parágrafos viram linhas em branco
                corpo = corpo[:40] + "\u00a0\u200b " + corpo[40:]  # nbsp e espaço invisível
                mexidas.append("João Marcus  10:32\n" + corpo + "\nEditado\n\U0001F44D 1")
            mexidas.append(mexidas[0])  # parte colada duas vezes
            self.rng.shuffle(mexidas)
            texto = "\n".join(mexidas)
            if modo == "u":
                texto = unicodedata.normalize("NFD", texto)
            _, avisos, rest = _restaurar(texto)
            self.assertEqual(avisos, [])
            self.assertEqual(rest, container)

    def test_parte_faltando_e_corrompida(self):
        cam = self.arquivo("t.txt", self.texto_aleatorio(30000))
        container, partes = _partes([cam], "u", max_chars=3000, max_bytes=8000)
        n = len(partes)
        cab, corpo = partes[1].split("\n", 1)
        estragada = cab + "\n" + ("一" if corpo[5] != "一" else "丁") + corpo[1:]
        col, avisos, rest = _restaurar("\n".join([partes[0], estragada] + partes[3:]))
        self.assertIsNone(rest)
        self.assertTrue(any("CRC" in a for a in avisos), avisos)
        conj = list(col.conjuntos.values())[0]
        self.assertEqual(conj.faltando(), [2, 3])
        self.assertEqual(tp._faixas(conj.faltando()), "2-3")
        # reenviadas, completa
        novas, avisos = col.adicionar(partes[1] + partes[2])
        self.assertEqual(avisos, [])
        self.assertEqual(sorted(i for _, i in novas), [2, 3])
        self.assertEqual(tp.abrir_blob(conj.blob()), container)
        self.assertEqual(conj.total, n)

    def test_mensagem_cortada(self):
        cam = self.arquivo("t.bin", os.urandom(5000))
        _, partes = _partes([cam], "u", max_chars=3000, max_bytes=8000)
        _, avisos, rest = _restaurar(partes[0][:-200])
        self.assertIsNone(rest)
        self.assertTrue(any("incompleta" in a for a in avisos), avisos)

    def test_txt_sem_utf8(self):
        cam = self.arquivo("t.bin", os.urandom(500))
        _, partes = _partes([cam], "u")
        texto = partes[0].encode("cp1252", "replace").decode("cp1252")
        _, avisos, rest = _restaurar(texto)
        self.assertIsNone(rest)
        self.assertTrue(any("UTF-8" in a for a in avisos), avisos)


class TestCompressao(Base):
    def test_todos_os_metodos_voltam(self):
        dados = self.texto_aleatorio(5000) + bytes(range(256)) * 20
        for nome, func in tp._candidatos(dados, "exaustivo"):
            with self.subTest(metodo=nome):
                self.assertEqual(tp.descomprimir(func()), dados)

    def test_escolhe_o_menor(self):
        dados = self.texto_aleatorio(20000)
        comp, _ = tp.comprimir_melhor(dados, "normal")
        menores = min(len(f()) for _, f in tp._candidatos(dados, "normal"))
        self.assertEqual(len(comp), menores)
        self.assertLess(len(comp), len(dados) / 4)

    def test_incompressivel_nao_cresce_muito(self):
        dados = os.urandom(50000)
        comp, metodo = tp.comprimir_melhor(dados, "normal")
        self.assertLessEqual(len(comp), len(dados) + 1)

    def test_sha_confere(self):
        container = b"\x00" + tp._str("a.txt") + b"conteudo"
        comp, _ = tp.comprimir_melhor(container, "rapido")
        blob = bytearray(tp.montar_blob(container, comp, "u"))
        blob[2] ^= 0xFF  # estraga o SHA guardado
        with self.assertRaises(tp.ErroTeamspack):
            tp.abrir_blob(bytes(blob))


class TestPastasEZip(Base):
    def test_pasta(self):
        raiz = os.path.join(self.tmp, "proj")
        arquivos = {
            "proj/a.py": self.texto_aleatorio(500),
            "proj/sub/b.py": self.texto_aleatorio(300),
            "proj/sub/ção ç.txt": "acentuação".encode("utf-8"),
            "proj/bin/x.dat": os.urandom(3000),
            "proj/vazio.txt": b"",
        }
        for rel, d in arquivos.items():
            self.arquivo(rel, d)
        os.makedirs(os.path.join(raiz, "pasta_vazia"))
        container, partes = _partes([raiz], "u")
        _, _, rest = _restaurar("".join(partes))
        destino = os.path.join(self.tmp, "saida")
        tp.extrair_container(rest, destino)
        for rel, d in arquivos.items():
            with open(os.path.join(destino, rel), "rb") as f:
                self.assertEqual(f.read(), d)
        self.assertTrue(os.path.isdir(os.path.join(destino, "proj", "pasta_vazia")))
        with self.assertRaises(tp.ErroTeamspack):  # não sobrescreve sem pedir
            tp.extrair_container(rest, destino)
        tp.extrair_container(rest, destino, sobrescrever=True)

    def test_zip_expandido(self):
        cam = os.path.join(self.tmp, "pacote.zip")
        conteudo = {"pasta/": b"", "pasta/a.txt": self.texto_aleatorio(2000), "b.bin": os.urandom(800)}
        with zipfile.ZipFile(cam, "w") as zf:
            zf.comment = b"comentario"
            for nome, d in conteudo.items():
                zi = zipfile.ZipInfo(nome, (2021, 5, 17, 13, 45, 22))
                zi.compress_type = zipfile.ZIP_STORED if nome == "b.bin" else zipfile.ZIP_DEFLATED
                zf.writestr(zi, d)
        _, partes = _partes([cam], "u", expandir_zip=True)
        _, _, rest = _restaurar("".join(partes))
        destino = os.path.join(self.tmp, "saida")
        (criado,) = tp.extrair_container(rest, destino)
        with zipfile.ZipFile(criado) as zf, zipfile.ZipFile(cam) as orig:
            self.assertIsNone(zf.testzip())
            self.assertEqual(zf.comment, b"comentario")
            for a, b in zip(orig.infolist(), zf.infolist()):
                self.assertEqual((a.filename, a.date_time, a.compress_type, a.CRC),
                                 (b.filename, b.date_time, b.compress_type, b.CRC))
                self.assertEqual(orig.read(a), zf.read(b))

    def test_caminho_inseguro(self):
        for ruim in ("../fora.txt", "/etc/x", "C:/x", "a/../../x"):
            container = bytes([tp.TIPO_VARIOS]) + tp._uvarint(1) + tp._str(ruim) + tp._uvarint(2) + b"x"
            with self.assertRaises(tp.ErroTeamspack):
                tp.extrair_container(container, self.tmp)


class TestAreaTransferencia(Base):
    def test_monitorar(self):
        cam = self.arquivo("t.txt", self.texto_aleatorio(20000))
        container, partes = _partes([cam], "u", max_chars=3000, max_bytes=8000)
        sequencia = ["algo antigo", partes[2], partes[2], "", partes[0]] + partes[1:]

        class Falsa:
            def colar(self):
                return sequencia.pop(0) if len(sequencia) > 1 else sequencia[0]

        col = tp.Coletor()
        log = []
        self.assertTrue(tp.monitorar(Falsa(), col, log.append, dormir=lambda s: None))
        self.assertEqual(tp.abrir_blob(col.completos()[0].blob()), container)

    def test_copiar_partes(self):
        copiados, perguntas = [], []

        class Falsa:
            def copiar(self, t):
                copiados.append(t)

        partes = [(1, 3, "a"), (2, 3, "b"), (3, 3, "c")]
        self.assertTrue(tp.copiar_partes(partes, log=lambda *a: None, perguntar=perguntas.append, area=Falsa()))
        self.assertEqual(copiados, ["a", "b", "c"])
        self.assertEqual(len(perguntas), 2)  # não espera depois da última


class TestLinhaDeComando(Base):
    def rodar(self, *args):
        r = subprocess.run([sys.executable, SCRIPT] + list(args), cwd=self.tmp,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return r.returncode, r.stdout.decode("utf-8", "replace")

    def test_enviar_e_receber(self):
        dados = self.texto_aleatorio(40000) + os.urandom(150000)
        self.arquivo("relatorio.dat", dados)
        cod, saida = self.rodar("enviar", "relatorio.dat", "--max-chars", "20000", "--max-bytes", "50000")
        self.assertEqual(cod, 0, saida)
        self.assertIn("Verificação:  OK", saida)
        pasta = os.path.join(self.tmp, "relatorio_teams")
        nomes = sorted(os.listdir(pasta))
        self.assertGreater(len(nomes), 1)
        # quem recebe cola tudo num único .txt, fora de ordem
        textos = [tp._ler_texto(os.path.join(pasta, n)) for n in nomes]
        with open(os.path.join(self.tmp, "msgs.txt"), "w", encoding="utf-8") as f:
            f.write("\n\nFulano 09:12\n".join(reversed(textos)))
        cod, saida = self.rodar("receber", "msgs.txt", "-o", "rec")
        self.assertEqual(cod, 0, saida)
        with open(os.path.join(self.tmp, "rec", "relatorio.dat"), "rb") as f:
            self.assertEqual(f.read(), dados)
        # pasta de partes também serve de entrada; sem --sobrescrever não pisa no arquivo
        cod, saida = self.rodar("receber", "relatorio_teams", "-o", "rec")
        self.assertNotEqual(cod, 0)
        self.assertIn("já existe", saida)

    def test_receber_incompleto(self):
        self.arquivo("x.bin", os.urandom(30000))
        self.assertEqual(self.rodar("enviar", "x.bin", "--max-chars", "4000", "-q")[0], 0)
        pasta = os.path.join(self.tmp, "x_teams")
        nomes = sorted(os.listdir(pasta))
        os.remove(os.path.join(pasta, nomes[1]))
        cod, saida = self.rodar("receber", "x_teams")
        self.assertEqual(cod, 1)
        self.assertIn("faltam as partes 2", saida)
        self.assertIn("--partes 2", saida)

    def test_enviar_pasta_atual_nao_inclui_saida(self):
        self.arquivo("a.txt", b"oi" * 100)
        self.arquivo("b.txt", b"tchau" * 100)
        for _ in range(2):
            self.assertEqual(self.rodar("enviar", ".", "-q", "-o", "saida_teams")[0], 0)
        col = tp.Coletor()
        for n in os.listdir(os.path.join(self.tmp, "saida_teams")):
            col.adicionar(tp._ler_texto(os.path.join(self.tmp, "saida_teams", n)))
        _, itens, _ = tp.ler_container(tp.abrir_blob(col.completos()[0].blob()))
        nomes = sorted(rel.split("/", 1)[1] for rel, _ in itens)
        self.assertEqual(nomes, ["a.txt", "b.txt"])


if __name__ == "__main__":
    unittest.main()
