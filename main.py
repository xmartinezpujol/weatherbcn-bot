import requests
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
import pytz
import logging
import matplotlib.pyplot as plt

load_dotenv()  # carga variables de .env
debug = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-bcn")

# Mapear códigos AEMET a nubes
AEMET_CLOUD_MAPPING = {
    "11": {"alta": 0, "media": 0, "baja": 0},  # despejado
    "11n": {"alta": 0, "media": 0, "baja": 0},  # despejado noche
    "12": {"alta": 0, "media": 0.2, "baja": 0},  # poco nuboso
    "12n": {"alta": 0, "media": 0.2, "baja": 0},
    "17": {"alta": 1, "media": 0, "baja": 0},  # nubes altas
    "17n": {"alta": 1, "media": 0, "baja": 0},
    "18": {"alta": 0, "media": 1, "baja": 0},  # nubes medias
    "18n": {"alta": 0, "media": 1, "baja": 0},
    "19": {"alta": 0, "media": 0, "baja": 1},  # nubes bajas
    "19n": {"alta": 0, "media": 0, "baja": 1},
    # agregar otros códigos según documentación AEMET si aparecen
}

AEMET_API_KEY =os.getenv("AEMET_API_KEY")
TELEGRAM_TOKEN =os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID =os.getenv("TELEGRAM_CHAT_ID")
SCORE_THRESHOLD = 0.5

if not (AEMET_API_KEY and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
    logger.error("Faltan variables de entorno: AEMET_API_KEY, TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
    raise SystemExit(1)

AEMET_HEADERS = {"api_key": AEMET_API_KEY}

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, json=payload, timeout=10)
    try:
        r.raise_for_status()
    except Exception as e:
        logger.exception("Error enviando mensaje a Telegram: %s", e)
    return r.json()


def compute_hour_conditions(hour_entry):
    estado = hour_entry.get("estadoCielo", [])
    if not estado:
        cloud_vals = {"alta": 0, "media": 0, "baja": 0}
    else:
        code = estado[0].get("value", "")
        cloud_vals = AEMET_CLOUD_MAPPING.get(code, {"alta": 0, "media": 0, "baja": 0})

    try:
        precip = float(hour_entry.get("precipitacion", [{}])[0].get("value", 0))
    except:
        precip = 0.0

    na, nm, nb = cloud_vals["alta"], cloud_vals["media"], cloud_vals["baja"]

    # Score cielo rojizo (solo nubes)
    sky_score = (0.6 * na) + (0.3 * nm) - (0.8 * nb)
    sky_score = max(0.0, min(1.0, sky_score))

    # Score lluvia
    rain_score = 1.0 if precip > 0 else 0.0

    details = {
        "nubes_altas": na,
        "nubes_medias": nm,
        "nubes_bajas": nb,
        "precipitacion": precip,
    }

    return sky_score, rain_score, details

def fetch_aemet_hourly_forecast():
    codigo_municipio = "08019"  # Barcelona
    url = f"https://proxy-aemet-production.up.railway.app/aemet/{codigo_municipio}"
    r = requests.get(url, headers=AEMET_HEADERS, timeout=15)
    r.raise_for_status()
    meta = r.json()
    datos_url = meta.get("datos")
    if not datos_url:
        raise RuntimeError("No se encontró URL de datos horarios en AEMET")
    rr = requests.get(datos_url, timeout=15)
    rr.raise_for_status()
    return rr.json()

def plot_sunset_forecast(sky_scores, rain_scores, title="Evolución Sunset"):
    hours = sorted(sky_scores.keys())
    sky_vals = [sky_scores[h] for h in hours]
    rain_vals = [rain_scores[h] for h in hours]

    plt.figure(figsize=(12,5))
    plt.plot(hours, sky_vals, marker='o', color='orange', label="Score Cielo")
    plt.bar(hours, rain_vals, width=0.3, color='skyblue', alpha=0.5, label="Lluvia")
    plt.xlabel("Hora")
    plt.ylabel("Score / Precipitación")
    plt.title(title)
    plt.xticks(hours)
    plt.ylim(0, 1.2)
    plt.legend()
    plt.grid(True)
    plt.show()

def analyze_day_forecast(forecast_json):
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)
    local_date = now.date()

    dias = forecast_json[0]['prediccion']['dia']
    dia_obj = next((d for d in dias if datetime.fromisoformat(d['fecha']).date() == local_date), None)
    if not dia_obj:
        logger.info("No se encontró la fecha objetivo en la predicción")
        return

    # Horas de orto y ocaso
    orto_hour = int(dia_obj.get("orto", "07:00").split(":")[0])
    ocaso_hour = int(dia_obj.get("ocaso", "17:00").split(":")[0])

    # Determinar horas a evaluar cielo rojizo
    sky_hours = []
    if now.hour < orto_hour:  # Antes del amanecer -> evaluar amanecer + atardecer
        sky_hours += list(range(max(0, orto_hour-1), orto_hour+2))
        sky_hours += list(range(ocaso_hour-1, min(23, ocaso_hour+2)+1))
    elif now.hour < ocaso_hour:  # Después del amanecer pero antes del atardecer -> solo atardecer
        sky_hours += list(range(ocaso_hour-1, min(23, ocaso_hour+2)+1))
    # Si ya pasó el atardecer, no evaluamos cielos rojizos
    sky_hours = sorted(set(sky_hours))

    # Determinar horas a evaluar lluvia
    rain_start_hour = max(8, now.hour)
    rain_end_hour = 22
    rain_hours = list(range(rain_start_hour, rain_end_hour+1))

    # Construir entries por hora
    hourly_entries = {}
    for h in set(sky_hours + rain_hours):
        ph = f"{h:02d}"
        estado = next((e for e in dia_obj.get('estadoCielo', []) if e['periodo'] == ph), None)
        precip = next((e for e in dia_obj.get('precipitacion', []) if e['periodo'] == ph), None)
        hour_entry = {}
        hour_entry['estadoCielo'] = [estado] if estado else []
        hour_entry['precipitacion'] = [precip] if precip else [{'value': '0', 'periodo': ph}]
        hourly_entries[h] = hour_entry

    # Calcular scores
    sky_scores, rain_scores, details = {}, {}, {}
    for h, entry in hourly_entries.items():
        s, r, d = compute_hour_conditions(entry)
        sky_scores[h] = s
        rain_scores[h] = r
        details[h] = d

    # Detectar cielo espectacular
    sky_alert_hours = [h for h in sky_hours if sky_scores[h] >= SCORE_THRESHOLD]

    # Detectar intervalos de lluvia ≥2h seguidas
    rain_alert_hours = []
    consecutive = 0
    for h in rain_hours:
        if rain_scores[h]:
            consecutive += 1
            if consecutive >= 2:
                rain_alert_hours.extend(range(h-1, h+1))
        else:
            consecutive = 0
    rain_alert_hours = sorted(set(rain_alert_hours))

    # Enviar alertas
    if sky_alert_hours or rain_alert_hours:
        messages = []
        if sky_alert_hours:
            messages.append(f"🌅🌇 Cielo espectacular entre horas: {', '.join(str(h) for h in sky_alert_hours)}")
        if rain_alert_hours:
            messages.append(f"🌧️ Posible lluvia entre horas: {', '.join(str(h) for h in rain_alert_hours)}")
        send_telegram_message("\n".join(messages))

    # Log técnico
    tech_lines = [f"Informe técnico {local_date.isoformat()}"]
    for h in sorted(hourly_entries.keys()):
        d = details[h]
        tech_lines.append(f"{h:02d}: score cielo {sky_scores[h]:.2f}, lluvia {rain_scores[h]:.0f}, "
                          f"NA {d['nubes_altas']:.2f}, NM {d['nubes_medias']:.2f}, NB {d['nubes_bajas']:.2f}, P {d['precipitacion']}")
    logger.info("\n" + "\n".join(tech_lines))

    # Plot solo en debug
    if debug:
        plot_sunset_forecast(sky_scores, rain_scores, f"Informe técnico {local_date}")


if __name__ == '__main__':
    tz = pytz.timezone("Europe/Madrid")
    now = datetime.now(tz)

    if debug:
        logger.info("Modo DEBUG activado: los gráficos se mostrarán")
    else:
        logger.info("Modo producción: no se mostrarán gráficos")

    try:
        fj = fetch_aemet_hourly_forecast()
        analyze_day_forecast(fj)
    except Exception as e:
        logger.exception("Error al obtener/analisar predicción AEMET: %s", e)

