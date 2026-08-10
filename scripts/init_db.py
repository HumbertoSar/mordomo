"""Leva o banco de DATABASE_URL até a última revisão do Alembic. Uso: make db-init

Antes isto rodava `create_all`. Não roda mais: `create_all` cria tabelas que
faltam mas nunca altera as que existem, e o banco já tem dado real da família.
A baseline em migrations/versions/ cria o schema inteiro num banco vazio, então
este script serve tanto para instalar do zero quanto para atualizar."""

from mordomo.db.migracoes import aplicar_migracoes
from mordomo.db.session import engine
from mordomo.plataforma import preparar


def main() -> None:
    if aplicar_migracoes():
        print(f"Banco em dia (alembic head) em {engine.url.render_as_string(hide_password=True)}")
    else:
        print("Alembic indisponível — nada foi aplicado.")


if __name__ == "__main__":
    preparar()  # psycopg async não roda no ProactorEventLoop do Windows
    main()
