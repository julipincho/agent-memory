"""Cliente de la memoria de DreamFactory (solo biblioteca estándar: Windows, hooks de Claude, cola, panel).

Garantías:
- La identidad la pone el servidor según el token (%LOCALAPPDATA%/DreamFactory/memoria/credenciales/<agente>.token).
- Nada se informa como guardado sin respuesta OK del servidor. Si PostgreSQL/WSL no responde, el comando va a una
  cola local durable (outbox JSONL con fsync) y se informa PENDIENTE; `vaciar` la reenvía con la misma clave_idem
  (el servidor no lo aplica dos veces). Lo rechazado por contrato/permisos pasa a rechazados.jsonl (no se borra).
- WSL se apaga sola cuando nadie la usa: el cliente la despierta con un proceso acotado (`sleep`) que la mantiene
  viva un rato; no cambia configuración de Windows ni deja procesos sin límite de tiempo.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

URL = os.environ.get("DFMEM_URL", "http://127.0.0.1:8790")
DIR = Path(os.environ.get("DFMEM_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DreamFactory" / "memoria")
# Optional (Windows + WSL only): keep a WSL distro awake while agents use the memory. Unset = disabled.
DISTRO = os.environ.get("DFMEM_WSL_DISTRO", "")
DISTRO_USER = os.environ.get("DFMEM_WSL_USER", "")
VIGILIA_S = 1800
# Background flushes: on Windows prefer pythonw.exe next to the current interpreter (no console window).
PYTHON = os.environ.get("DFMEM_PYTHONW") or str(Path(sys.executable).with_name("pythonw.exe"))
OCULTO = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW para cada subprocess
AQUI = Path(__file__).resolve().parent


class ErrorMemoria(Exception):
    def __init__(self, codigo: int, error: str, mensaje: str):
        super().__init__(f"[{codigo} {error}] {mensaje}")
        self.codigo, self.error, self.mensaje = codigo, error, mensaje

    @property
    def sin_conexion(self) -> bool:
        return self.codigo in (0, 502, 503, 504)


def _escribir_atomico(ruta: Path, texto: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_name(f"{ruta.name}.{os.getpid()}.tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, ruta)


def token(agente: str) -> str:
    if os.environ.get("DFMEM_TOKEN"):
        return os.environ["DFMEM_TOKEN"].strip()
    ruta = DIR / "credenciales" / f"{agente}.token"
    try:
        return ruta.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ErrorMemoria(401, "sin_credencial", f"No hay credencial para «{agente}» en {ruta}") from e


def http(metodo: str, ruta: str, cuerpo: dict | None = None, agente: str | None = None, timeout: float = 15) -> dict:
    cabeceras = {"Content-Type": "application/json"}
    if agente:
        cabeceras["Authorization"] = f"Bearer {token(agente)}"
    datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8") if cuerpo is not None else None
    pedido = urllib.request.Request(URL + ruta, data=datos, headers=cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            info = json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            info = {}
        raise ErrorMemoria(e.code, info.get("error", "http"), info.get("mensaje", str(info)[:300])) from e
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise ErrorMemoria(0, "sin_conexion", f"No responde {URL} ({e}); nada se guardó en PostgreSQL") from e


# ---------------------------------------------------------------- WSL despierta (acotado)

def _pid_vivo(pid: int) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    codigo = ctypes.c_ulong()
    k32.GetExitCodeProcess(h, ctypes.byref(codigo))
    k32.CloseHandle(h)
    return codigo.value == 259   # STILL_ACTIVE


def _desacoplado(argv: list[str]) -> int:
    banderas = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB. Sin DETACHED_PROCESS: con él, cada
        # hijo de consola (wsl.exe, git) abría su propia ventana de cmd.
        banderas = OCULTO | 0x00000200 | 0x01000000
    try:
        p = subprocess.Popen(argv, creationflags=banderas, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        p = subprocess.Popen(argv, creationflags=banderas & ~0x01000000, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return p.pid


def mantener_wsl() -> None:
    """Lanza (si hace falta) un `sleep` acotado en la distro: la despierta y la mantiene viva VIGILIA_S segundos."""
    if os.name != "nt" or not DISTRO:
        return
    ruta = DIR / "vigilia.json"
    try:
        v = json.loads(ruta.read_text(encoding="utf-8"))
        if _pid_vivo(int(v.get("pid", 0))) and v.get("hasta", 0) - time.time() > 300:
            return
    except (OSError, ValueError):
        pass
    pid = _desacoplado(["wsl.exe", "-d", DISTRO, *(["-u", DISTRO_USER] if DISTRO_USER else []), "--", "sleep", str(VIGILIA_S)])
    _escribir_atomico(ruta, json.dumps({"pid": pid, "hasta": time.time() + VIGILIA_S}))


def disponible(espera_s: float = 0) -> bool:
    """¿Responde la API? Con espera > 0, despierta WSL y espera a que PostgreSQL y la API estén listos."""
    limite = time.time() + espera_s
    despierta = False
    while True:
        try:
            http("GET", "/v1/salud", timeout=3)
            if espera_s:
                mantener_wsl()   # renueva la vigilia mientras se usa
            return True
        except ErrorMemoria:
            if time.time() >= limite:
                return False
            if not despierta:
                mantener_wsl()
                despierta = True
            time.sleep(1.5)


# ---------------------------------------------------------------- cola local (outbox)

def _cola(agente: str) -> Path:
    return DIR / "cola" / f"{agente}.jsonl"


def encolar(agente: str, tipo: str, datos: dict, clave: str | None = None) -> str:
    clave = clave or f"{tipo}-{uuid.uuid4()}"
    linea = json.dumps({"tipo": tipo, "clave_idem": clave, "datos": datos, "encolado": time.time()},
                       ensure_ascii=False, default=str)
    ruta = _cola(agente)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(linea + "\n")
        f.flush()
        os.fsync(f.fileno())
    return clave


def pendientes(agente: str) -> int:
    try:
        return sum(1 for l in _cola(agente).read_text(encoding="utf-8").splitlines() if l.strip())
    except OSError:
        return 0


class _Candado:
    def __init__(self, ruta: Path):
        self.ruta, self.fd = ruta, None

    def __enter__(self):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(50):
            try:
                self.fd = os.open(self.ruta, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:   # candado huérfano de un proceso muerto o viejo
                    viejo = int(self.ruta.read_text() or 0)
                    if not _pid_vivo(viejo) or time.time() - self.ruta.stat().st_mtime > 300:
                        self.ruta.unlink(missing_ok=True)
                        continue
                except (OSError, ValueError):
                    pass
                time.sleep(0.2)
        raise ErrorMemoria(0, "ocupado", f"Otro proceso está vaciando la cola ({self.ruta})")

    def __exit__(self, *_):
        os.close(self.fd)
        self.ruta.unlink(missing_ok=True)


def vaciar(agente: str, espera_s: float = 0) -> dict:
    """Envía la cola local en lotes. Devuelve {enviados, rechazados, pendientes}."""
    ruta = _cola(agente)
    if not pendientes(agente):
        return {"enviados": 0, "rechazados": 0, "pendientes": 0}
    if not disponible(espera_s):
        return {"enviados": 0, "rechazados": 0, "pendientes": pendientes(agente), "error": "sin_conexion"}
    enviados = rechazados = 0
    with _Candado(ruta.with_suffix(".candado")):
        lineas = [l for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()]
        restantes: list[str] = []
        for i in range(0, len(lineas), 100):
            tanda = lineas[i:i + 100]
            comandos = [{k: v for k, v in json.loads(l).items() if k in ("tipo", "clave_idem", "datos")} for l in tanda]
            try:
                r = http("POST", "/v1/lote", {"comandos": comandos}, agente, timeout=60)["resultados"]
            except ErrorMemoria as e:
                if not e.sin_conexion:
                    raise
                restantes += lineas[i:]
                break
            por_clave = {x["clave_idem"]: x for x in r}
            for l, c in zip(tanda, comandos):
                x = por_clave.get(c["clave_idem"])
                if x and x["ok"]:
                    enviados += 1
                elif x and x.get("codigo") not in (0, 503):
                    rechazados += 1
                    with open(DIR / "cola" / "rechazados.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps({"agente": agente, "comando": json.loads(l), "error": x}, ensure_ascii=False) + "\n")
                else:
                    restantes.append(l)
        actuales = [l for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()]
        nuevas = actuales[len(lineas):]   # lo que otro proceso encoló mientras tanto
        _escribir_atomico(ruta, "".join(x + "\n" for x in restantes + nuevas))
    return {"enviados": enviados, "rechazados": rechazados, "pendientes": pendientes(agente)}


def vaciar_en_segundo_plano(agente: str, espera_s: int = 90) -> None:
    _desacoplado([PYTHON if os.path.exists(PYTHON) else sys.executable, str(AQUI / "dfmem.py"), "--agente", agente,
                  "vaciar", "--esperar", str(espera_s), "--silencioso"])


# ---------------------------------------------------------------- operaciones de alto nivel

def comando(agente: str, tipo: str, datos: dict, clave: str | None = None, espera_s: float = 0,
            encolar_si_falla: bool = True) -> dict:
    """Envía y confirma. Sin conexión: encola y devuelve {pendiente: True} — NO confirmado."""
    clave = clave or f"{tipo}-{uuid.uuid4()}"
    if pendientes(agente):   # mantener el orden: primero lo viejo
        try:
            vaciar(agente, espera_s)
        except ErrorMemoria:
            pass
    try:
        if espera_s and not disponible(espera_s):
            raise ErrorMemoria(0, "sin_conexion", "La API de memoria no respondió a tiempo")
        return {**http("POST", "/v1/comando", {"tipo": tipo, "clave_idem": clave, "datos": datos}, agente),
                "confirmado": True}
    except ErrorMemoria as e:
        if not (e.sin_conexion and encolar_si_falla):
            raise
        encolar(agente, tipo, datos, clave)
        return {"pendiente": True, "confirmado": False, "clave_idem": clave, "motivo": e.mensaje}


def contexto(agente: str, proyecto: str, espera_s: float = 0, para: str | None = None, **pedido) -> dict:
    """Paquete de contexto (para= otro agente: solo orquestador). Sin servicio: el último en caché, no confirmado."""
    if para:
        pedido["agente"] = para
    cache = DIR / "cache" / f"{agente}-{para or agente}-{proyecto}.txt"
    try:
        if espera_s and not disponible(espera_s):
            raise ErrorMemoria(0, "sin_conexion", "La API de memoria no respondió a tiempo")
        r = http("POST", "/v1/contexto", {"proyecto": proyecto, **{k: v for k, v in pedido.items() if v is not None}},
                 agente, timeout=30)
        _escribir_atomico(cache, r["texto"])
        return {**r, "confirmado": True}
    except ErrorMemoria as e:
        if not e.sin_conexion:
            raise
        try:
            viejo = cache.read_text(encoding="utf-8")
            fecha = time.strftime("%d/%m %H:%M", time.localtime(cache.stat().st_mtime))
        except OSError:
            viejo, fecha = "", "nunca"
        aviso = (f"⚠ MEMORIA PERSISTENTE NO DISPONIBLE ({e.mensaje}). Paquete en caché local del {fecha}: puede estar "
                 f"desactualizado. Lo que guardes queda PENDIENTE en la cola local hasta que vuelva.")
        return {"texto": aviso + ("\n" + viejo if viejo else ""), "confirmado": False, "tokens": 0}
