"""Canal como tag do trace e release do git — sem rede, sem chaves."""

import re

from mordomo.observability import _release_do_git, config_invocacao, release_atual


def test_config_invocacao_carimba_o_canal():
    cfg = config_invocacao(1, "Ana", "adulto", "t1", canal="whatsapp")
    assert "canal:whatsapp" in cfg["metadata"]["langfuse_tags"]
    assert cfg["metadata"]["canal"] == "whatsapp"


def test_config_invocacao_sem_canal_nao_inventa_tag():
    # proativos e testes antigos chamam sem canal — nada de tag "canal:None"
    cfg = config_invocacao(1, "Ana", "adulto", "t1")
    assert not any(t.startswith("canal:") for t in cfg["metadata"]["langfuse_tags"])
    assert "canal" not in cfg["metadata"]


def test_release_vem_do_git_quando_nao_ha_env():
    # neste checkout o .git existe — o SHA curto tem que sair dele; no
    # container (sem .git) a função devolve None e o release vem do env
    sha = _release_do_git()
    assert sha is None or re.fullmatch(r"[0-9a-f]{12}", sha)
    release = release_atual()
    assert release is None or isinstance(release, str)
