"""Camada de LEITURA dos eventos de produto — a "gestão à vista".

`product_events` guarda fatos (tokens, ms, ok/erro). Tudo que é métrica derivada
— custo em dólar, p95, taxa de sucesso — nasce aqui, na leitura. Assim o preço
de um modelo pode mudar sem reescrever o histórico."""
