"""Integração NF-e (modelo 55) com a SEFAZ-GO usando a biblioteca PyNFe.

As URLs dos webservices ficam dentro do PyNFe (por UF); aqui só informamos uf='GO'.
COMECE SEMPRE EM HOMOLOGAÇÃO ("homologacao": true no config.json).
"""
import datetime
import json
import os
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# Pasta do .exe (quando empacotado) ou do script: config.json, xml/, certificado e pdv.db ficam aqui.
BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
XML_DIR = BASE / "xml"

EXEMPLO = {
    "homologacao": True,
    "uf": "GO",
    "serie": "1",
    "certificado_pfx": "certificado.pfx",
    "certificado_senha": "",  # prefira a variável de ambiente PDV_CERT_PASSWORD
    "emitente": {
        "razao_social": "MINHA EMPRESA LTDA",
        "nome_fantasia": "MINHA EMPRESA",
        "cnpj": "00000000000000",
        "ie": "000000000",
        "im": "",
        "cnae": "4712100",
        "crt": "1",  # 1 = Simples Nacional (único regime implementado aqui)
        "logradouro": "Rua Exemplo",
        "numero": "100",
        "bairro": "Centro",
        "municipio": "Goiania",
        "cod_municipio": "5208707",
        "cep": "74000000",
    },
    # NF-e modelo 55 exige endereço completo do destinatário.
    "cliente_endereco_padrao": {
        "logradouro": "Rua Exemplo",
        "numero": "S/N",
        "bairro": "Centro",
        "municipio": "Goiania",
        "cod_municipio": "5208707",
        "uf": "GO",
        "cep": "74000000",
    },
}


def carregar_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(EXEMPLO, indent=2, ensure_ascii=False), encoding="utf-8")
        raise RuntimeError("config.json criado com valores de exemplo. Preencha-o e tente novamente.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _dec(v, casas="0.01"):
    return Decimal(str(v)).quantize(Decimal(casas), rounding=ROUND_HALF_UP)


def _texto(resp):
    if isinstance(resp, (str, bytes)):
        return resp.decode() if isinstance(resp, bytes) else resp
    if hasattr(resp, "text"):
        return resp.text
    from lxml import etree
    return etree.tostring(resp, encoding="unicode")


def emitir_nfe(venda, itens, numero, pagamentos):
    """venda: dict(total, cliente_doc, cliente_nome); itens: lista de dict
    (codigo, descricao, ncm, unidade, qtd, preco); pagamentos: lista de (tPag, valor), ex.: ('17', 50.0). Retorna dict com status/chave/protocolo/motivo."""
    from lxml import etree
    from pynfe.entidades.cliente import Cliente
    from pynfe.entidades.emitente import Emitente
    from pynfe.entidades.fonte_dados import _fonte_dados
    from pynfe.entidades.notafiscal import NotaFiscal
    from pynfe.processamento.assinatura import AssinaturaA1
    from pynfe.processamento.comunicacao import ComunicacaoSefaz
    from pynfe.processamento.serializacao import SerializacaoXML
    from pynfe.utils.flags import CODIGO_BRASIL

    cfg = carregar_config()
    em, ce = cfg["emitente"], cfg["cliente_endereco_padrao"]
    homolog = cfg.get("homologacao", True)
    uf = cfg.get("uf", "GO")
    senha = os.environ.get("PDV_CERT_PASSWORD") or cfg.get("certificado_senha", "")
    pfx = str(BASE / cfg["certificado_pfx"])  # caminho relativo é resolvido a partir da pasta do app

    if em.get("crt", "1") != "1":
        raise NotImplementedError("Apenas Simples Nacional (CRT 1, CSOSN 102) está implementado. "
                                  "Para Regime Normal é preciso informar CST/alíquotas de ICMS, PIS e COFINS.")

    # PyNFe registra as entidades em um repositório global; limpa para não acumular notas.
    objs = getattr(_fonte_dados, "_objetos", None)
    if isinstance(objs, (dict, list)):
        objs.clear()

    emitente = Emitente(
        razao_social=em["razao_social"], nome_fantasia=em["nome_fantasia"], cnpj=em["cnpj"],
        codigo_de_regime_tributario=em["crt"], inscricao_estadual=em["ie"],
        inscricao_municipal=em.get("im", ""), cnae_fiscal=em["cnae"],
        endereco_logradouro=em["logradouro"], endereco_numero=em["numero"],
        endereco_bairro=em["bairro"], endereco_municipio=em["municipio"],
        endereco_uf=uf, endereco_cep=em["cep"], endereco_pais=CODIGO_BRASIL,
    )

    doc = venda["cliente_doc"]
    nome = venda["cliente_nome"] or "CONSUMIDOR"
    if homolog:  # exigência da SEFAZ para o destinatário em homologação
        nome = "NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"
    cliente = Cliente(
        razao_social=nome, tipo_documento="CNPJ" if len(doc) == 14 else "CPF",
        numero_documento=doc, indicador_ie=9,  # 9 = não contribuinte
        endereco_logradouro=ce["logradouro"], endereco_numero=ce["numero"],
        endereco_bairro=ce["bairro"], endereco_municipio=ce["municipio"],
        endereco_cod_municipio=ce["cod_municipio"], endereco_uf=ce["uf"],
        endereco_cep=ce["cep"], endereco_pais=CODIGO_BRASIL,
    )

    agora = datetime.datetime.now()
    nf = NotaFiscal(
        emitente=emitente, cliente=cliente, uf=uf, natureza_operacao="VENDA",
        forma_pagamento=0, modelo=55, serie=cfg.get("serie", "1"),
        numero_nf=str(numero), data_emissao=agora, data_saida_entrada=agora,
        tipo_documento=1, municipio=em["cod_municipio"], tipo_impressao_danfe=1,
        forma_emissao="1", cliente_final=1, indicador_destino=1,
        indicador_intermediador=0, finalidade_emissao="1", processo_emissao="0",
        transporte_modalidade_frete=9, informacoes_adicionais_interesse_fisco="",
        totais_tributos_aproximado=Decimal("0.00"), valor_troco=_dec(venda.get("troco") or 0),
    )

    for it in itens:
        q, p = _dec(it["qtd"], "0.0001"), _dec(it["preco"])
        total = _dec(q * p)
        nf.adicionar_produto_servico(
            codigo=it["codigo"], descricao=it["descricao"], ncm=it["ncm"], cfop="5102",
            unidade_comercial=it["unidade"], ean="SEM GTIN", ean_tributavel="SEM GTIN",
            quantidade_comercial=q, valor_unitario_comercial=p, valor_total_bruto=total,
            unidade_tributavel=it["unidade"], quantidade_tributavel=q,
            valor_unitario_tributavel=p, ind_total=1,
            icms_modalidade="102", icms_origem=0, icms_csosn="102",
            pis_modalidade="07", cofins_modalidade="07", valor_tributos_aprox="0.00",
        )

    for cod, valor in pagamentos:  # 01 dinheiro, 03 crédito, 04 débito, 17 PIX
        nf.adicionar_pagamento(t_pag=cod, v_pag=_dec(valor), ind_pag=0,
                               tp_integra="2" if cod in ("03", "04") else "",  # 2 = cartão não integrado
                               t_band="")  # bandeira/CNPJ/autorização são opcionais com tpIntegra=2

    nfe = SerializacaoXML(_fonte_dados, homologacao=homolog).exportar()
    xml = AssinaturaA1(pfx, senha).assinar(nfe)
    envio = ComunicacaoSefaz(uf, pfx, senha, homolog).autorizacao(
        modelo="nfe", nota_fiscal=xml)
    texto = _texto(envio[1])

    XML_DIR.mkdir(exist_ok=True)
    (XML_DIR / f"envio_{numero}.xml").write_bytes(etree.tostring(xml))
    (XML_DIR / f"retorno_{numero}.xml").write_text(texto, encoding="utf-8")

    def pega(tag, base=texto):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", base, re.S)
        return m.group(1) if m else ""

    prot = re.search(r"<protNFe.*?</protNFe>", texto, re.S)
    bloco = prot.group(0) if prot else texto
    cstat = pega("cStat", bloco)
    return {
        "status": "AUTORIZADA" if cstat in ("100", "150") else "REJEITADA",
        "cstat": cstat,
        "motivo": pega("xMotivo", bloco),
        "chave": pega("chNFe", bloco),
        "protocolo": pega("nProt", bloco),
    }


def _tag(base, nome):
    m = re.search(rf"<{nome}>(.*?)</{nome}>", base, re.S)
    return m.group(1) if m else ""


def cancelar_nfe(chave, protocolo, justificativa):
    """Cancela uma NF-e autorizada (evento 110111). Em Goiás só é aceito até 24 h após a
    autorização e se a mercadoria não circulou (RCTE-GO, art. 167-H)."""
    from lxml import etree
    from pynfe.entidades.evento import EventoCancelarNota
    from pynfe.entidades.fonte_dados import _fonte_dados
    from pynfe.processamento.assinatura import AssinaturaA1
    from pynfe.processamento.comunicacao import ComunicacaoSefaz
    from pynfe.processamento.serializacao import SerializacaoXML

    just = justificativa.strip()
    if len(just) < 15:
        raise ValueError("A justificativa do cancelamento precisa ter ao menos 15 caracteres.")
    cfg = carregar_config()
    homolog = cfg.get("homologacao", True)
    uf = cfg.get("uf", "GO")
    senha = os.environ.get("PDV_CERT_PASSWORD") or cfg.get("certificado_senha", "")
    pfx = str(BASE / cfg["certificado_pfx"])

    objs = getattr(_fonte_dados, "_objetos", None)
    if isinstance(objs, (dict, list)):
        objs.clear()
    evento = EventoCancelarNota(
        cnpj=cfg["emitente"]["cnpj"], chave=chave, data_emissao=datetime.datetime.now(),
        uf=uf, protocolo=protocolo, justificativa=just[:255])
    xml_evento = SerializacaoXML(_fonte_dados, homologacao=homolog).serializar_evento(evento)
    xml = AssinaturaA1(pfx, senha).assinar(xml_evento)
    envio = ComunicacaoSefaz(uf, pfx, senha, homolog).evento(modelo="nfe", evento=xml)
    texto = _texto(envio[1])

    XML_DIR.mkdir(exist_ok=True)
    (XML_DIR / f"cancelamento_envio_{chave}.xml").write_bytes(etree.tostring(xml))
    (XML_DIR / f"cancelamento_retorno_{chave}.xml").write_text(texto, encoding="utf-8")

    ret = re.search(r"<retEvento.*?</retEvento>", texto, re.S)
    bloco = ret.group(0) if ret else texto
    cstat = _tag(bloco, "cStat")
    return {  # 135 = evento registrado e vinculado; 155 = cancelamento homologado fora de prazo
        "status": "CANCELADA" if cstat in ("135", "155") else "REJEITADA",
        "cstat": cstat, "motivo": _tag(bloco, "xMotivo"), "protocolo": _tag(bloco, "nProt"),
    }
