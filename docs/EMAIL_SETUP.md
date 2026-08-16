# Configurar el acceso al correo de alertas

El bot lee las alertas que los portales inmobiliarios envían por email.
Necesita permiso de **solo lectura** sobre un buzón dedicado.

## Antes de nada: usa un dominio propio, no una Gmail gratuita

El 14/08/2026 Google deshabilitó `pisos.charles@gmail.com` por completo —
no el token, la cuenta — alegando que podía haber sido creada por un
programa automático. Un buzón dedicado, recién creado, que solo recibe
correo de portales y nunca envía, encaja exactamente en el patrón que
Google barre. El bot estuvo 44 horas ciego y ninguna alerta de esos días
existe ya en ninguna parte.

Con una dirección en un dominio propio (Google Workspace) eso no pasa, y
además desbloquea la diferencia que de verdad importa: la pantalla de
consentimiento puede marcarse como **Interna**, y una app interna:

- no necesita verificación de Google,
- no caduca sus refresh tokens a los 7 días como las apps externas en
  modo de prueba,
- no está sujeta a los barridos antiabuso de las cuentas gratuitas.

Las instrucciones de abajo asumen ese caso. Si de verdad no hay dominio
disponible, el backend `imap` lee cualquier buzón con host, usuario y
contraseña de aplicación, y evita Google del todo.

## Por qué OAuth y no una contraseña normal

Google está retirando progresivamente las contraseñas de aplicación (IMAP).
OAuth siempre funciona y además permite pedir un permiso más restringido:
`gmail.readonly`. Con ese permiso el bot **puede leer y nada más** — no puede
enviar, borrar ni modificar nada del buzón.

## 1. Crear las credenciales en Google Cloud (una sola vez, ~10 min)

1. **console.cloud.google.com** → arriba, selector de proyecto →
   **"Proyecto nuevo"** → nombre `pisos-bot` → **Crear**
2. Buscador de arriba: **"Gmail API"** → **Habilitar**
3. Menú ☰ → **"APIs y servicios"** → **"Pantalla de consentimiento de OAuth"**
   - Tipo de usuario: **Interna** ← solo aparece si el proyecto pertenece a
     un dominio de Workspace, y es la opción que evita la verificación y la
     caducidad del token a los 7 días
   - Nombre de la aplicación: `pisos-bot`
   - Correo de asistencia y de contacto: tu correo
   - Guardar y continuar hasta el final
   - Con **Interna** no hay lista de usuarios de prueba: cualquier cuenta
     del dominio puede autorizar
4. Menú ☰ → **"APIs y servicios"** → **"Credenciales"**
   - **"Crear credenciales"** → **"ID de cliente de OAuth"**
   - Tipo de aplicación: **Aplicación de escritorio**
   - Nombre: `pisos-bot`
   - **Crear** → aparecen **ID de cliente** y **Secreto de cliente**

## 2. Guardar las credenciales

En `~/sell_analysis/.env`:

```
GMAIL_CLIENT_ID=xxxxx.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=GOCSPX-xxxxx
```

## 3. Autorizar

En la máquina que tiene navegador (el Mac donde corre el bot):

```bash
cd ~/sell_analysis
./venv/bin/python main.py mail-auth
```

Se abre el navegador → elige el buzón de alertas → **Continuar**.

Google avisará de que la app "no está verificada": es tu propia app, así que
pulsa **"Configuración avanzada"** → **"Ir a pisos-bot (no seguro)"**.

El token queda en `data/gmail_token.json` con permisos `600`.

## 4. Comprobar

```bash
./venv/bin/python main.py mail-check   # verifica el acceso
./venv/bin/python main.py mail-once    # lee las alertas y muestra resultados
```

## Alternativa: IMAP con contraseña de aplicación

Si en tu cuenta sí están disponibles, es más simple. En `config.yaml`:

```yaml
email_ingest:
  backend: imap
```

Y en `.env`:

```
MAIL_USER=tu-buzon@gmail.com
MAIL_APP_PASSWORD=abcdefghijklmnop
```

## Alertas a crear en los portales

Créalas **anchas** — filtra el bot, no el portal. Si estrechas mucho en el
portal te pierdes oportunidades que el bot habría detectado.

- Operación: comprar / viviendas
- Zona: Barcelona (o los distritos que te interesen)
- Precio máximo: 350.000 €
- Habitaciones: 2 o más
- Frecuencia: **inmediata o diaria**, nunca semanal
- No marques ascensor / planta / estado — eso lo evalúa el bot

Portales soportados: Idealista, Fotocasa, Habitaclia, Servihabitat, Solvia,
Altamira, Aliseda. Añadir uno nuevo es una entrada en `PORTAL_SPECS`
(`ingest/email_parsers.py`), no código nuevo.
