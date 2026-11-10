"""Invariantes sobre todo lo que dejaron las demás pruebas (corre al final por orden alfabético)."""
from conftest import sql
from servidor import admin


def test_ningun_paquete_supera_el_presupuesto_sin_avisar():
    filas = sql("SELECT count(*) n FROM consumo_contexto WHERE tokens_entregados > presupuesto "
                "AND NOT excedido_por_criticos")
    assert filas[0].n == 0


def test_integridad_referencial_y_de_historia():
    r = admin.verificar()
    assert r["integro"], r["problemas"]
