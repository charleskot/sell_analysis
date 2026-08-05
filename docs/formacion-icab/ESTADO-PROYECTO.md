# formacion.icab.es — Estado del proyecto

> **Para:** Charles · **De:** Jared (+ su Claude) · **Fecha:** 31/07/2026
> Resumen ejecutivo de todo lo hecho desde el HANDOFF del 20/07, qué está desplegado, qué está
> pendiente y dónde está cada cosa. Complementa (no sustituye) a `HANDOFF.md` y `docs/CMS-I18N.md`.

---

## TL;DR

La web ha pasado de maqueta con contenido ficticio a **plataforma bilingüe (ES/CA) gestionada
íntegramente desde Payload**, con el **catálogo real de 8 másters del ICAB** (datos y precios
oficiales), rediseño visual completo, SEO técnico hecho y el formulario de leads **funcionando por
fin en producción** (antes se perdían — ver §6). Todo desplegado en `formacio-des` salvo el último
bloque (URLs localizadas), que está committeado en local y **pendiente de push + deploy**.

---

## 1. Rediseño visual "ICAB Contemporáneo" (20-21/07) — ✅ desplegado

- Sistema de diseño nuevo: rampa granate con gradientes cinematográficos y luz ámbar, titulares en
  **Fraunces** (serif display), etiquetas monospace numeradas, tarjetas glass, grano de película,
  contenido centrado a 1200px. Basado en un benchmark de 7 escuelas (ESADE, IE, ISDE, UPF-BSM,
  Garrigues, LSE, Ironhack) — matrices en `docs/estudio/benchmark-escuelas.md`.
- **Home**: hero con vídeo-loop cinemático (Higgsfield, con degradación elegante a imagen fija),
  **buscador de especialización** ("Quiero especializarme en… → Ver máster"), catálogo con chips
  de filtro por área, secciones de metodología, "La IA en el aula", admisión y testimonios.
- **Landing de programa** (plantilla única para todos los másters): key facts box con precio a la
  vista (pago único + cuotas), "El programa de un vistazo" (descripción + cifras), metodología,
  temario en acordeón, claustro con retrato por persona, certificaciones/convenios, tabla de
  precios con el colectivo destacado, testimonios, FAQs reales y convocatoria.
- Logo oficial ICAB (escudo) en header, footer, sello del hero y favicon.
- ~20 imágenes provisionales generadas con Higgsfield (estilo cinematográfico coherente) — a
  sustituir por fotos reales cuando Formació pase el dosier.

## 2. Catálogo real (21/07) — ✅ desplegado

- Scrapeado el catálogo oficial completo (14 programas 2026-27) con el patrón catálogo →
  pre-landing → landing: `docs/estudio/catalogo-icab-2026-27.md` (fechas, horarios, temarios,
  precios, colaboraciones, condiciones de pago — la referencia de contenido del proyecto).
- Estudio de demanda de mercado (Hays 2026, IurisTalent, CGPJ, rankings):
  `docs/estudio/demanda-mercado.md`. Cruce demanda × catálogo → **8 másters publicados**:
  Reestructuraciones e Insolvencia, Fiscal, Laboral y RRHH, TMT, Compliance, Familia y
  Sucesiones, Abogacía Penal, IA y Derecho.
- **Precios oficiales** (3.900→3.510€ hasta 4.950€; Laboral con tarifa propia 4.050-5.100€) y
  letra pequeña real: 10% Servei de Formació i Documentació, 10% pago único, matrícula + 8
  cuotas, becas 25%, Títol d'Especialista con 80% de asistencia, cancelación 15 días.
- Oportunidad detectada para proponer al ICAB: **máster de Extranjería** (demanda +50% tras el
  Reglamento 2025, sin competencia de escuelas élite; no existe en su catálogo).

## 3. Auditoría técnica (21/07) — ✅ desplegado

`docs/estudio/audit-2026-07-21.md`. Lighthouse sobre producción: **96-99 desktop / 86 móvil /
100 SEO**. Los 7 problemas encontrados, corregidos: enlace /blog roto, contraste AA, dimensiones
de imágenes, robots.txt, sitemap.xml, Open Graph + canonical, y **JSON-LD `Course`** por máster
(elegible para rich results de cursos).

## 4. CMS + web bilingüe (27/07) — ✅ desplegado

- **Todo el contenido vive en Payload** (ya no hay textos hardcodeados): colecciones `masters`,
  `faqs`, `testimonios` + global `home`, con **localización nativa ES/CA** (castellano por
  defecto; selector *Locale* en el admin). La landing es plantilla única: crear un máster en el
  CMS lo publica solo, en los dos idiomas.
- Traducción completa al catalán (nombres oficiales del catálogo; **pendiente de validación por
  alguien del ICAB**).
- Rutas `/es` y `/ca`, selector CA·ES en el header, textos de interfaz en `src/lib/ui.ts`,
  ISR de 60s (editar en el admin → la web se actualiza sola, sin deploy).
- SEO bilingüe: hreflang es/ca/x-default, canonical por idioma, sitemap con alternates, JSON-LD
  con `inLanguage`.
- Guía de uso y de despliegue (con los gotchas aprendidos): **`docs/CMS-I18N.md`** ← léela antes
  de tocar migraciones o desplegar.
- Fix funcional: el `colectivo` del formulario usa valores estables (`icab_5`…) independientes
  del idioma — en catalán antes no enviaba.

## 5. URLs localizadas (30/07) — ⚠️ committeado en LOCAL, **SIN push y SIN desplegar**

Cierre de la issue #3 tras el feedback de skolaerp (lista de slugs CA confirmada por él el 29/07):

- **Slug por idioma** como campo localizado del CMS: `/es/master/derecho-fiscal` ↔
  `/ca/master/dret-fiscal`, `/es/master/abogacia-penal` ↔ `/ca/master/advocacia-penal`…
- **Mapa central de rutas** `src/lib/rutas.ts`: cada página declara su segmento en cada idioma
  (`/es/gracias` ↔ `/ca/gracies`); de ahí salen enlaces, sitemap, hreflang y redirecciones.
  `master` igual en ambos idiomas y sin acentos (criterio de skolaerp).
- El selector de idioma resuelve el equivalente real (slug incluido); cada URL existe **solo** en
  su idioma (la versión con slug del otro idioma da 404 — sin contenido duplicado).
- **Migración con traspaso de datos** (`20260730_123417_slug_localizado`): la autogenerada rompía
  producción (columna NOT NULL sobre filas existentes); la reescrita copia los slugs por idioma
  antes de endurecer la columna, y está validada sobre una BD con datos como los de producción.

**Commits locales pendientes de push**: `4c5fd49` (feature) y `a003e48` (docs).
**Despliegue pendiente**: tar de `src` → `echo y | pnpm payload migrate` → `pnpm seed` →
limpiar rutas viejas si las hubiera → `pnpm build` → `pm2 restart` (procedimiento exacto y
gotchas en `docs/CMS-I18N.md` §despliegue).

## 6. Hallazgo importante del 27/07 — la BD de producción estaba vacía

Al preparar la migración descubrimos que la base de datos de `formacio-des` tenía **cero tablas**:
la web funcionaba porque el contenido era código, pero **`/api/solicitudes` fallaba y cualquier
lead enviado desde producción se perdía** desde el primer deploy. Resuelto con la migración
baseline; verificado creando (y borrando) un lead real. Desde el 27/07 los leads persisten.

## 7. Estado de las issues de GitHub

| # | Título | Estado real |
|---|---|---|
| 1 | Configuración ficha de programa / CMS | Hecho y desplegado (comentado en la issue con detalle) |
| 2 | Integración Web + CMS | Cerrada |
| 3 | Multilingüe CA/ES | Hecho; el remate de URLs localizadas espera push+deploy (§5) |
| 4 | Colecciones CMS + contenido real | Hecho y desplegado (comentado en la issue) |
| 12 | Sincronización ediciones (GesColAd) | **Bloqueada**: sin respuesta de Sergio Trabanco a las 6 dudas de la API |
| 5-11 | HubSpot / Stripe / sync inscripciones | Asignadas a Charles |

## 8. Pendientes y bloqueos externos

- **Push + deploy de las URLs localizadas** (§5) — lo único técnico pendiente de nuestro lado.
- **Nginx del servidor sin activar** a propósito: Jared pidió esperar el permiso del ICAB. Hasta
  entonces la web solo se ve por túnel SSH o desde la red interna del Colegio.
- Bloqueos ICAB: respuestas API GesColAd (Sergio), alta Stripe (KYC), dosier de programas y
  fotos reales (Eva), licencia HubSpot (Xavier), validación del catalán, textos legales
  (aviso legal / privacidad / cookies — los enlaces del footer esperan contenido).
- Fase 2 acordada en el blueprint: folleto PDF real, WhatsApp/llamada agendada,
  vídeo-testimonios, sesiones informativas con fecha, quiz "¿qué máster encaja contigo?".

## 9. Mapa de documentación

| Documento | Qué contiene |
|---|---|
| `HANDOFF.md` | Accesos, servidor, flujo de deploy original (de Charles, sigue vigente) |
| `docs/CMS-I18N.md` | **Cómo usar el CMS + cómo desplegar con migraciones + gotchas** |
| `docs/ESTADO-PROYECTO.md` | Este documento |
| `docs/estudio/` | Catálogo real scrapeado, benchmark, demanda, blueprint, auditoría |
| `docs/superpowers/specs` y `plans` | Spec y plan del rediseño aprobados por Jared |

Admin del CMS: `/admin` (el primer usuario de producción se crea al entrar; de momento acceso
solo Mestral/WeRise, decisión de Jared del 27/07). Dev local: Postgres portable en puerto 5433 +
`pnpm seed` + `pnpm dev -p 3007` (el 3000 suele estar ocupado por el túnel y el 3001 por otro
proyecto del NucBox).
