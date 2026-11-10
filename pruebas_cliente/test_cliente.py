"""Pruebas de extremo a extremo del lado de los agentes contra el servicio REAL (proyecto aislado «pruebas»).

Simulan a Claude Code: invocan hooks_claude.py como subproceso con el JSON por stdin, y usan dfmem como lo
haría cualquier agente. Cubren: continuidad entre sesiones (cerrar y reabrir), compactación, PostgreSQL caído
(PENDIENTE explícito + reenvío idempotente), permisos, operación incierta (código 3) y presupuesto de tokens.
Solo biblioteca estándar. Uso: python pruebas_cliente/test_cliente.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
ZONA = AQUI / "zona"
PY = sys.executable
sys.path.insert(0, str(RAIZ))
import cliente  # noqa: E402


class Entorno:
    """Estado local aislado (DFMEM_DIR temporal) con copia de las credenciales reales."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="dfmem-prueba-"))
        shutil.copytree(cliente.DIR / "credenciales", self.dir / "credenciales")
        self.env = {**os.environ, "DFMEM_DIR": str(self.dir), "PYTHONIOENCODING": "utf-8"}

    def hook(self, evento: str, entrada: dict) -> dict | None:
        r = subprocess.run([PY, str(RAIZ / "hooks_claude.py"), evento], input=json.dumps(entrada).encode(),
                           capture_output=True, env=self.env, cwd=ZONA, timeout=120)
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        return json.loads(r.stdout) if r.stdout.strip() else None

    def dfmem(self, *args: str, agente: str = "claude", env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([PY, str(RAIZ / "dfmem.py"), "--agente", agente, "--proyecto", "pruebas", *args],
                              capture_output=True, text=True, encoding="utf-8", env={**self.env, **(env or {})},
                              cwd=ZONA, timeout=120)

    def pendientes(self) -> int:
        cola = self.dir / "cola" / "claude.jsonl"
        return len([l for l in cola.read_text(encoding="utf-8").splitlines() if l.strip()]) if cola.exists() else 0

    def cerrar(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


@unittest.skipUnless(cliente.disponible(60), "la memoria no responde")
class PruebasCliente(unittest.TestCase):
    def setUp(self) -> None:
        self.e = Entorno()
        self.addCleanup(self.e.cerrar)

    def test_claude_cierra_y_reabre_y_continua(self):
        tarea = f"T{uuid.uuid4().hex[:6]}"
        s1 = f"prueba-{uuid.uuid4()}"
        pk = self.e.hook("session-start", {"session_id": s1, "source": "startup", "cwd": str(ZONA)})
        texto = pk["hookSpecificOutput"]["additionalContext"]
        self.assertIn("<<MEMORIA DREAMFACTORY", texto)
        self.assertIn("proyecto pruebas", texto)
        r = self.e.dfmem("tarea", "usar", tarea)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.e.hook("post-tool-use", {"session_id": s1, "cwd": str(ZONA), "tool_name": "Edit",
                                      "tool_input": {"file_path": str(ZONA / "LEEME.txt")}})
        r = self.e.dfmem("checkpoint", "--haciendo", "migrar el HUD a señales", "--hecho", "extraer señales",
                         "--pendiente", "probar recorrido", "--reanudar", "src/ui/hud.gd:120",
                         "--prueba", "test_ui=OK")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.e.hook("stop", {"session_id": s1, "cwd": str(ZONA), "stop_hook_active": False})
        self.e.hook("session-end", {"session_id": s1, "cwd": str(ZONA), "reason": "prompt_input_exit"})
        self.assertEqual(self.e.pendientes(), 0, "session-end debe confirmar lo encolado")
        # «cerrar y reabrir el entorno»: sesión nueva, estado local nuevo (otra máquina/proceso), misma tarea
        nuevo = Entorno()
        self.addCleanup(nuevo.cerrar)
        r = nuevo.dfmem("tarea", "usar", tarea)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("src/ui/hud.gd:120", r.stdout)
        self.assertIn("probar recorrido", r.stdout)
        self.assertIn("test_ui=OK", r.stdout)
        s2 = f"prueba-{uuid.uuid4()}"
        pk2 = nuevo.hook("session-start", {"session_id": s2, "source": "startup", "cwd": str(ZONA)})
        texto2 = pk2["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Reanudar desde: src/ui/hud.gd:120", texto2)   # lo semántico no lo tapa el cierre automático
        self.assertIn("Después (checkpoint automático", texto2)

    def test_compactacion_reinyecta_contexto_y_guarda_checkpoint(self):
        s = f"prueba-{uuid.uuid4()}"
        self.e.hook("session-start", {"session_id": s, "source": "startup", "cwd": str(ZONA)})
        self.e.hook("post-tool-use", {"session_id": s, "cwd": str(ZONA), "tool_name": "Write",
                                      "tool_input": {"file_path": str(ZONA / "nuevo.gd")}})
        self.e.hook("pre-compact", {"session_id": s, "cwd": str(ZONA), "trigger": "auto"})
        self.assertEqual(self.e.pendientes(), 0, "pre-compact debe confirmar el checkpoint automático")
        pk = self.e.hook("session-start", {"session_id": s, "source": "compact", "cwd": str(ZONA)})
        texto = pk["hookSpecificOutput"]["additionalContext"]
        self.assertIn("compactacion", texto)
        self.assertIn("automático: antes de compactar", texto)
        self.assertIn("nuevo.gd", texto)

    def test_postgres_caido_queda_pendiente_y_se_reenvia_sin_duplicar(self):
        titulo = f"Guardado sin conexión {uuid.uuid4().hex[:8]}"
        caido = {"DFMEM_URL": "http://127.0.0.1:9"}
        r = self.e.dfmem("--esperar", "0", "recordar", "--clave", f"offline-{uuid.uuid4().hex[:8]}", "--tipo", "hecho",
                         "--titulo", titulo, env=caido)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("PENDIENTE", r.stderr)
        self.assertEqual(self.e.pendientes(), 1)
        linea = (self.e.dir / "cola" / "claude.jsonl").read_text(encoding="utf-8")
        r = self.e.dfmem("vaciar")
        self.assertEqual((r.returncode, json.loads(r.stdout)["enviados"]), (0, 1), r.stderr)
        (self.e.dir / "cola" / "claude.jsonl").write_text(linea, encoding="utf-8")   # ACK perdido: se reenvía igual
        r = self.e.dfmem("vaciar")
        self.assertEqual(json.loads(r.stdout)["enviados"], 1)
        r = self.e.dfmem("buscar", titulo)
        self.assertEqual(r.stdout.count(titulo), 1, r.stdout)   # una sola memoria, una sola revisión

    def test_permisos_por_http(self):
        r = self.e.dfmem("recordar", "--clave", "x", "--tipo", "hecho", "--titulo", "no debería", agente="tortu")
        self.assertEqual(r.returncode, 1)
        self.assertIn("denegado", r.stderr.lower())
        r = self.e.dfmem("recordar", "--alcance", "global", "--clave", "x", "--tipo", "regla", "--titulo", "t",
                         agente="codex")
        self.assertEqual(r.returncode, 1)

    def test_operacion_incierta_devuelve_codigo_3(self):
        clave = f"subir-build-{uuid.uuid4().hex[:6]}"
        r = self.e.dfmem("operacion", "iniciar", clave, "--tipo", "publicacion", "--descripcion", "subir a itch")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.e.dfmem("operacion", "iniciar", clave, "--tipo", "publicacion")
        self.assertEqual(r.returncode, 3)
        self.assertIn("NO EJECUTAR", r.stdout)
        r = self.e.dfmem("operacion", "resolver", clave, "--estado", "confirmada", "--resultado", '{"url": "x"}')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_presupuesto_respetado(self):
        r = self.e.dfmem("--json", "contexto", "--presupuesto", "300", "--operacion", "busqueda")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d["tokens"] <= d["presupuesto"] or d["excedido_por_criticos"], d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
