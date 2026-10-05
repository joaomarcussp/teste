# teamspack

Transforma arquivos e pastas em mensagens de texto para enviar pelo Teams e
restaura o original **byte a byte** do outro lado. É um único script Python
(3.8+), só com a biblioteca padrão. Quem envia e quem recebe precisam dele.

## Por que fica pequeno

1. **Compressão:** testa LZMA2 com vários parâmetros, filtro BCJ para
   executáveis, bzip2 e deflate, em paralelo, e fica com o menor resultado.
2. **Codificação densa:** o modo padrão escreve 15 bits por caractere, usando
   32.768 ideogramas CJK/Hangul. O base64 escreve só 6. O texto fica com cara de
   chinês, mas os caracteres são letras comuns do Unicode, que o Teams aceita.
3. **Mensagens cheias:** cada parte fica abaixo de dois limites ao mesmo tempo,
   caracteres e bytes UTF-8.

| Modo                    | Dados por mensagem (padrão) | 3 MB comprimidos viram |
|-------------------------|-----------------------------|------------------------|
| unicode (padrão)        | cerca de 59 KB              | 51 mensagens           |
| `--ascii` (base64)      | cerca de 24 KB              | 123 mensagens          |

Os limites padrão são **33.000 caracteres e 95.000 bytes por mensagem**, com
folga sobre os cerca de 35 mil caracteres e 100 KB do Teams. Ajuste com
`--max-chars` e `--max-bytes`.

## Enviar

```
python teamspack.py enviar relatorio.pdf
python teamspack.py enviar minha_pasta outro_arquivo.xlsx
python teamspack.py enviar relatorio.pdf --copiar
```

- O primeiro comando gera `relatorio_teams/parte_01de12.txt`, e assim por diante.
  Abra cada arquivo, copie tudo (Ctrl+A, Ctrl+C) e envie como uma mensagem.
- Com `--copiar`, o script coloca a parte 1 na área de transferência. Você cola
  no Teams, envia, tecla Enter e ele copia a parte 2, e assim por diante.
- Se faltar alguma parte do outro lado, reenvie só ela:
  `python teamspack.py copiar relatorio_teams --partes 4,9-11`

Opções úteis:

- `--ascii`: use se o Teams (ou outro caminho) estragar os caracteres Unicode.
- `--exaustivo`: testa muito mais combinações de compressão (mais lento, às vezes ganha uns %).
- `--rapido`: um método só, para arquivos grandes.
- `--expandir-zip`: para `.zip`, `.docx`, `.xlsx`, `.pptx`. Descompacta e recomprime
  muito melhor. O zip recriado tem **o mesmo conteúdo** (arquivos, nomes, datas),
  mas não é idêntico byte a byte. Sem essa opção, o arquivo volta idêntico.
- `-v`: mostra o tamanho que cada método de compressão conseguiu.

## Receber

Escolha uma das três formas:

- **Capturando da área de transferência (mais prático):**
  ```
  python teamspack.py receber --colar
  ```
  Deixe rodando. No Teams, selecione o texto de cada mensagem e dê Ctrl+C, em
  qualquer ordem. Quando chegar a última parte, o arquivo é restaurado sozinho.
- **Colando num .txt:** cole todas as mensagens num arquivo de texto salvo em
  **UTF-8**, uma depois da outra. Nome do remetente, horário e "Editado" no meio
  não atrapalham. Depois rode:
  ```
  python teamspack.py receber mensagens.txt -o destino
  ```
- **A partir de uma pasta:** `receber pasta_com_txts` também funciona.

## Garantias

- Cada parte tem um CRC32 e o conteúdo inteiro é conferido com SHA-256. Se algo
  vier errado, o script avisa qual parte refazer e nunca grava um arquivo errado.
- Antes de gravar as partes, o próprio `enviar` restaura o pacote na memória e
  compara com o original.
- Espaços, quebras de linha, espaços invisíveis e normalização Unicode
  introduzidos pelo copiar/colar são ignorados. Partes repetidas também.
- Não sobrescreve arquivos existentes sem `--sobrescrever`.

## Dicas

- Arquivos que já são comprimidos quase não diminuem: JPG, PNG, MP4, PDF, ZIP.
  Para .zip, .docx e .xlsx, use `--expandir-zip`. Para uma pasta de código,
  envie a pasta em vez de um .zip dela.
- No Teams, se uma mensagem longa aparecer cortada com "Ver mais", expanda antes
  de copiar. Se faltar o fim, o script avisa que a parte veio "incompleta".
- Se o Teams recusar a mensagem por tamanho, diminua os limites, por exemplo
  `--max-bytes 80000`.
- Antes do primeiro envio grande, teste com um arquivo pequeno.

## Testes

```
python -m unittest test_teamspack -v
```
