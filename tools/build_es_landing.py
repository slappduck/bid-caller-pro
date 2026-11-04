"""The Spanish landing page, /es/index.html, built from the English one.

    python3 tools/build_es_landing.py

Many concrete company owners and estimators read Spanish first. The landing
page is translated here as (English, Spanish) pairs applied to index.html, so
the Spanish page keeps every script, form, price and link of the English one
and can't silently fall behind it: if a phrase below is no longer in
index.html, the build stops and says which, and a test keeps the committed
page equal to what this builds. Change index.html, then update the pair.

The app opens in Spanish from here (/app.html?lang=es). The Terms and the
Privacy Policy are in English; the page says so where it links to them.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "curbcall_netlify_v4")
SRC = os.path.join(SITE, "index.html")
OUT = os.path.join(SITE, "es", "index.html")

# Applied in order; each English phrase must appear in index.html.
PAIRS = [
    # head
    ('<html lang="en">', '<html lang="es">'),
    ('<title>CurbCall Pro — Find Sidewalk, ADA &amp; Curb Bids Near You</title>',
     '<title>CurbCall Pro — Licitaciones de aceras, rampas ADA y bordillos cerca de usted</title>'),
    ('<meta name="description" content="CurbCall Pro finds live sidewalk, ADA ramp and curb &amp; gutter bids near you, then helps you win them: what the state paid for the same work, a target price, bid forms filled in, addendum alerts on your phone and ready-mix plants near the job." />',
     '<meta name="description" content="CurbCall Pro encuentra licitaciones abiertas de aceras, rampas ADA y bordillo y cuneta cerca de usted, y le ayuda a ganarlas: lo que pagó el estado por el mismo trabajo, un precio objetivo, formularios de licitación llenados, avisos de adendas en su teléfono y plantas de concreto premezclado cerca de la obra." />'),
    ('<link rel="canonical" href="https://curbcallpro.com/" />', '<link rel="canonical" href="https://curbcallpro.com/es/" />'),
    ('<meta property="og:url" content="https://curbcallpro.com/" />',
     '<meta property="og:url" content="https://curbcallpro.com/es/" />\n<meta property="og:locale" content="es_US" />'),
    ('<meta property="og:title" content="CurbCall Pro — Find Sidewalk, ADA &amp; Curb Bids Near You" />',
     '<meta property="og:title" content="CurbCall Pro — Licitaciones de aceras, rampas ADA y bordillos cerca de usted" />'),
    ('<meta property="og:description" content="We watch the bid pages of every city, county, school district and special district in your radius and surface the sidewalk, ADA-ramp and curb &amp; gutter work. More than 9,000 verified agency bid pages across 52 states." />',
     '<meta property="og:description" content="Vigilamos las páginas de licitaciones de cada ciudad, condado, distrito escolar y distrito especial en su radio y le mostramos el trabajo de aceras, rampas ADA y bordillo y cuneta. Más de 9,000 páginas de licitación verificadas en 52 estados." />'),
    ('<meta name="twitter:title" content="CurbCall Pro — Find Sidewalk, ADA &amp; Curb Bids Near You" />',
     '<meta name="twitter:title" content="CurbCall Pro — Licitaciones de aceras, rampas ADA y bordillos cerca de usted" />'),
    ('<meta name="twitter:description" content="Sidewalk, ADA-ramp and curb &amp; gutter bids from more than 9,000 verified agency bid pages across 52 states. Check your area free." />',
     '<meta name="twitter:description" content="Licitaciones de aceras, rampas ADA y bordillo y cuneta de más de 9,000 páginas de licitación verificadas en 52 estados. Revise su zona gratis." />'),
    # nav
    ('<a href="#how">How it works</a>\n      <a href="#features">Features</a>\n      <a href="#win">Pricing tools</a>\n      <a href="#pricing">Pricing</a>\n      <a href="/es/" lang="es">Español</a>\n      <a class="btn btn-amber nav-cta" href="/app.html">Launch App</a>',
     '<a href="#how">Cómo funciona</a>\n      <a href="#features">Funciones</a>\n      <a href="#win">Precios de obra</a>\n      <a href="#pricing">Planes</a>\n      <a href="/" lang="en">English</a>\n      <a class="btn btn-amber nav-cta" href="/app.html?lang=es">Abrir la app</a>'),
    # hero
    ('<span class="eyebrow">Concrete bid intelligence</span>', '<span class="eyebrow">Licitaciones de concreto</span>'),
    ('<h1>Win more<br><span class="hi">flatwork.</span></h1>', '<h1>Gane más<br><span class="hi">obra de concreto.</span></h1>'),
    ('<p class="lead">Tell CurbCall Pro your ZIP and it finds the live sidewalk, ADA ramp and curb &amp; gutter bids near you.<span class="lead-more"> It reads the government and municipal sites around you and pulls the work out automatically — so you spend your time pouring concrete, not hunting for it.</span></p>',
     '<p class="lead">Dele a CurbCall Pro su código postal y encuentra las licitaciones abiertas de aceras, rampas ADA y bordillo y cuneta cerca de usted.<span class="lead-more"> Lee los sitios del gobierno y de los municipios a su alrededor y saca el trabajo automáticamente, para que usted pase su tiempo colando concreto, no buscándolo.</span></p>'),
    ('placeholder="Your ZIP code"\n               aria-label="Your ZIP code"', 'placeholder="Su código postal"\n               aria-label="Su código postal"'),
    ('id="hero-zip-go">See bids near me</button>', 'id="hero-zip-go">Ver licitaciones cerca</button>'),
    ('<a class="btn btn-line btn-lg" href="/app.html">Start free — open the app</a>\n        <a class="btn btn-line btn-lg" href="#how">See how it works</a>',
     '<a class="btn btn-line btn-lg" href="/app.html?lang=es">Empiece gratis: abra la app</a>\n        <a class="btn btn-line btn-lg" href="#how">Vea cómo funciona</a>'),
    ('<div class="trust"><b><span class="dot"></span>Free 14-day trial</b><span>· no card required</span><span>· works on phone or desktop</span></div>',
     '<div class="trust"><b><span class="dot"></span>Prueba gratis de 14 días</b><span>· sin tarjeta</span><span>· en teléfono o computadora</span></div>'),
    ('<div class="maphead"><span>Scanning · 125 mi</span>\n        <span class="mlive"><span class="dot"></span>Live</span></div>',
     '<div class="maphead"><span>Buscando · 125 mi</span>\n        <span class="mlive"><span class="dot"></span>En vivo</span></div>'),
    ('<div class="mcount"><span id="mn">0</span> <span id="mnw">bids</span>\n          <small>found near Springfield, MO</small></div>',
     '<div class="mcount"><span id="mn">0</span> <span id="mnw">licitaciones</span>\n          <small>encontradas cerca de Springfield, MO</small></div>'),
    # how it works
    ('<span class="eyebrow">How it works</span>\n      <h2>From scan to quote in minutes</h2>\n      <p>You don\'t need to know a single website address. Tell CurbCall Pro where you are, and it does the digging.</p>',
     '<span class="eyebrow">Cómo funciona</span>\n      <h2>De la búsqueda al precio en minutos</h2>\n      <p>No necesita saber ninguna dirección de sitio web. Dígale a CurbCall Pro dónde está y él hace la búsqueda.</p>'),
    ('<h3>Set your area</h3><p>Enter a ZIP or let the app detect your location, then pick a radius from 10 to 125 miles.</p>',
     '<h3>Elija su zona</h3><p>Escriba un código postal o deje que la app detecte su ubicación, y escoja un radio de 10 a 125 millas.</p>'),
    ('<h3>Scan nearby towns</h3><p>It finds municipal and government bid pages across your radius and reads them automatically.</p>',
     '<h3>Busca en los pueblos cercanos</h3><p>Encuentra las páginas de licitación municipales y del gobierno en su radio y las lee automáticamente.</p>'),
    ('<h3>AI pulls the concrete jobs</h3><p>The AI surfaces real flatwork — sidewalk, ADA ramp, and curb &amp; gutter bids — with scope, deadline, and contact.</p>',
     '<h3>La IA saca los trabajos de concreto</h3><p>La IA muestra obra real de concreto (licitaciones de aceras, rampas ADA y bordillo y cuneta) con el alcance, la fecha límite y el contacto.</p>'),
    ('<h3>Price it and bid</h3><p>See what the same work went for, get a target price, and let the app fill in the bid form. You check it, sign it and send it.</p>',
     '<h3>Póngale precio y licite</h3><p>Vea cuánto se pagó por el mismo trabajo, obtenga un precio objetivo y deje que la app llene el formulario de licitación. Usted lo revisa, lo firma y lo envía.</p>'),
    # features
    ('<span class="eyebrow">Built for concrete crews</span>\n      <h2>Everything you need to find flatwork</h2>\n      <p>No spreadsheets, no endless tabs, no missed deadlines. Just the sidewalk, ADA, and curb jobs near you.</p>',
     '<span class="eyebrow">Hecho para cuadrillas de concreto</span>\n      <h2>Todo lo que necesita para encontrar obra de concreto</h2>\n      <p>Sin hojas de cálculo, sin un montón de pestañas, sin fechas perdidas. Solo los trabajos de aceras, rampas ADA y bordillos cerca de usted.</p>'),
    ('<h3>Location-based search</h3><p>Works anywhere in the country. Enter your ZIP or auto-detect, set a radius, and scan every town nearby.</p>',
     '<h3>Búsqueda por ubicación</h3><p>Funciona en todo el país. Escriba su código postal o detéctelo, fije un radio y busque en cada pueblo cercano.</p>'),
    ('<h3>Tuned for concrete</h3><p>The AI is pointed at sidewalk replacement, ADA ramp, and curb &amp; gutter work — not a generic pile of every trade.</p>',
     '<h3>Enfocado en concreto</h3><p>La IA busca reemplazo de aceras, rampas ADA y bordillo y cuneta, no un montón de todos los oficios.</p>'),
    ('<h3>Open-bid feed</h3><p>Only open bids show, and every new scan adds to your list — so leads build up instead of disappearing.</p>',
     '<h3>Lista de licitaciones abiertas</h3><p>Solo aparecen licitaciones abiertas, y cada búsqueda nueva se suma a su lista, así los trabajos se acumulan en vez de perderse.</p>'),
    ('<h3>Federal + local</h3><p>Pulls SAM.gov federal solicitations alongside local municipal bids, all filtered to your radius.</p>',
     '<h3>Federal y local</h3><p>Trae las solicitudes federales de SAM.gov junto con las licitaciones municipales, todo filtrado a su radio.</p>'),
    ("<h3>Sort by kind of work</h3><p>Narrow your list to sidewalk, ADA ramps, curb &amp; gutter, paving or drainage in one tap, across every town you cover. Star the ones you're chasing.</p>",
     '<h3>Ordene por tipo de trabajo</h3><p>Reduzca su lista a aceras, rampas ADA, bordillo y cuneta, pavimento o drenaje en un toque, en todos los pueblos que cubre. Marque con estrella las que quiere ganar.</p>'),
    ('<use href="#i-scale"/></svg></div><h3>See where you placed</h3><p>After bids open, the app reads the results when the agency posts them: your rank, how far off the winner you were, and their price on every line.</p>',
     '<use href="#i-scale"/></svg></div><h3>Vea en qué lugar quedó</h3><p>Después de la apertura, la app lee los resultados cuando la agencia los publica: su lugar, qué tan lejos quedó del ganador y el precio de él en cada renglón.</p>'),
    ('<h3>Every line starts priced</h3><p>At your own past prices once you have them, otherwise at what the work won for nearby, sized to your job: from state bid results across the lower 48, updated every month.</p>',
     '<h3>Cada renglón empieza con precio</h3><p>Con sus propios precios anteriores cuando ya los tiene; si no, con lo que se ganó por ese trabajo cerca de usted, ajustado al tamaño de su obra: de los resultados de licitación estatales en los 48 estados continentales, actualizado cada mes.</p>'),
    ("<h3>Should you bid it, and at what price</h3><p>How many contractors usually bid this kind of job there, who keeps winning it, a target price from what won at your job's quantities, and the items jobs like it usually pay for that are easy to miss, like ramp domes and removal.</p>",
     '<h3>¿Le conviene licitar, y a qué precio?</h3><p>Cuántos contratistas suelen licitar este tipo de trabajo ahí, quién lo sigue ganando, un precio objetivo según lo que ganó con las cantidades de su obra, y los conceptos que trabajos así suelen pagar y que son fáciles de olvidar, como los domos de las rampas y la demolición.</p>'),
    # win
    ('<span class="eyebrow">Then it helps you win it</span>\n      <h2>Price it right, file it right, never miss a change</h2>\n      <p>Finding the bid is half the job. The rest is the number, the paperwork and the addendum nobody told you about.</p>',
     '<span class="eyebrow">Y luego le ayuda a ganarla</span>\n      <h2>El precio correcto, los papeles correctos, ningún cambio perdido</h2>\n      <p>Encontrar la licitación es la mitad del trabajo. El resto es el número, los papeles y la adenda de la que nadie le avisó.</p>'),
    ("<h3>Bid forms filled in for you</h3><p>Enter your license, bonding and supplier details once. The app fills in the agency's bid form and lists what the bid asks for. You sign and submit it yourself.</p>",
     '<h3>Formularios de licitación llenados</h3><p>Escriba una sola vez sus datos de licencia, fianzas y proveedores. La app llena el formulario de la agencia y le dice lo que pide la licitación. Usted la firma y la entrega.</p>'),
    ("<h3>Addendum alerts on your phone</h3><p>Every saved bid is re-checked every six hours. An addendum or new document sends an alert, and you get a reminder the day before it's due.</p>",
     '<h3>Avisos de adendas en su teléfono</h3><p>Cada licitación guardada se revisa cada seis horas. Una adenda o un documento nuevo le manda un aviso, y recibe un recordatorio el día antes de la entrega.</p>'),
    ('<h3>Quotes to prime contractors</h3><p>Not bidding as the prime? See who took plans and send each one your concrete quote, written from your own prices, in a tap.</p>',
     '<h3>Cotizaciones al contratista principal</h3><p>¿No licita como contratista principal? Vea quién sacó los planos y mándele a cada uno su cotización de concreto, hecha con sus propios precios, en un toque.</p>'),
    ('<h3>Ready-mix near the job</h3><p>Plants close to the job, how many trucks it takes and what the concrete should cost, plus real quotes from other contractors nearby once there are enough.</p>',
     '<h3>Concreto premezclado cerca de la obra</h3><p>Las plantas cerca de la obra, cuántos camiones se necesitan y cuánto debe costar el concreto, más cotizaciones reales de otros contratistas cercanos cuando hay suficientes.</p>'),
    ('<p class="win-free">Want a feel for prices first? <a href="/concrete-cost/index.html">See concrete prices by state</a>, or <a href="/concrete-bids/index.html">open concrete bids by state</a>, free, no sign-up.</p>',
     '<p class="win-free">¿Quiere ver precios primero? <a href="/concrete-cost/index.html">Precios de concreto por estado</a> o <a href="/concrete-bids/index.html">licitaciones de concreto abiertas por estado</a>, gratis, sin registrarse (en inglés).</p>'),
    ('<h3>Not ready to try the app? Get open bids near you, free, every Monday.</h3>\n      <p>The open sidewalk, curb and ADA ramp bids within 50 miles of your ZIP, in one email a week. No account. One click to stop.</p>',
     '<h3>¿Todavía no quiere probar la app? Reciba gratis las licitaciones abiertas cerca de usted cada lunes.</h3>\n      <p>Las licitaciones abiertas de aceras, bordillos y rampas ADA a 50 millas de su código postal, en un correo a la semana (en inglés). Sin cuenta. Un clic para dejar de recibirlo.</p>'),
    ('<label>Email<input type="email"', '<label>Correo electrónico<input type="email"'),
    ('<label>ZIP code<input type="text" name="zip"', '<label>Código postal<input type="text" name="zip"'),
    ('<button type="submit" class="btn btn-amber">Send me the bids</button>', '<button type="submit" class="btn btn-amber">Mándenme las licitaciones</button>'),
    ('<p class="su-note">We email you a link to confirm first. <a href="/privacy.html">Privacy policy</a>.</p>',
     '<p class="su-note">Primero le mandamos un enlace para confirmar. <a href="/privacy.html">Política de privacidad</a> (en inglés).</p>'),
    # coverage
    ('<span class="eyebrow">Coverage</span>\n      <h2>Check your area first</h2>', '<span class="eyebrow">Cobertura</span>\n      <h2>Revise su zona primero</h2>'),
    ("<p>We hold <b>more than 9,000 verified agency bid pages</b> across 52 states and\n         territories &mdash; cities, counties, school districts and special districts.\n         Coverage is not even everywhere &mdash; see exactly what's near you before you\n         pay a cent.</p>",
     '<p>Tenemos <b>más de 9,000 páginas de licitación verificadas</b> en 52 estados y\n         territorios: ciudades, condados, distritos escolares y distritos especiales.\n         La cobertura no es igual en todas partes: vea exactamente lo que hay cerca de usted antes\n         de pagar un centavo.</p>'),
    ('placeholder="ZIP code or City, State"', 'placeholder="Código postal o Ciudad, Estado"'),
    ('<select id="cov-radius" aria-label="Search radius in miles"', '<select id="cov-radius" aria-label="Radio de búsqueda en millas"'),
    ('<button class="btn btn-amber" id="cov-go" type="button">Check</button>', '<button class="btn btn-amber" id="cov-go" type="button">Revisar</button>'),
    ("<h3 style=\"font-size:1rem;margin-bottom:0.3rem;\">Work for a city or county?</h3>\n      <p style=\"color:var(--text2);font-size:0.88rem;margin-bottom:0.8rem;\">\n        Post a sidewalk, ADA or concrete bid notice free and reach contractors already\n        searching your area. No account, no cost &mdash; and it doesn't replace your\n        legal notice.</p>\n      <a class=\"btn btn-line\" href=\"/post-a-bid.html\">Post a bid notice &rarr;</a>",
     '<h3 style="font-size:1rem;margin-bottom:0.3rem;">¿Trabaja para una ciudad o un condado?</h3>\n      <p style="color:var(--text2);font-size:0.88rem;margin-bottom:0.8rem;">\n        Publique gratis un aviso de licitación de aceras, ADA o concreto y llegue a los contratistas que ya\n        buscan en su zona. Sin cuenta, sin costo, y no reemplaza su\n        aviso legal.</p>\n      <a class="btn btn-line" href="/post-a-bid.html">Publicar un aviso (en inglés) &rarr;</a>'),
    ("<b>What that number is.</b> Government bid pages we've verified and read directly\n      every scan. On top of these, each scan also searches for county, school-district\n      and state-portal work that has no fixed page, and pulls federal solicitations from\n      SAM.gov &mdash; so it's a floor, not a ceiling.\n      <b>What it isn't.</b> A promise of open work. Concrete jobs are posted when they're\n      posted; in a sampling of live city bid pages, about 8% had sidewalk, ADA or concrete\n      work open at that moment. Denser areas surface more, rural areas fewer, and a wider\n      radius helps a lot. That's why the trial is free for 14 days with no card &mdash;\n      check your own area before deciding.",
     '<b>Qué es ese número.</b> Páginas de licitación del gobierno que hemos verificado y que leemos directamente\n      en cada búsqueda. Además, cada búsqueda busca trabajo de condados, distritos escolares\n      y portales estatales que no tiene una página fija, y trae las solicitudes federales de\n      SAM.gov, así que es un mínimo, no un máximo.\n      <b>Qué no es.</b> Una promesa de trabajo abierto. Los trabajos de concreto se publican cuando se\n      publican; en una muestra de páginas de licitación de ciudades, cerca del 8% tenía trabajo de aceras, ADA o concreto\n      abierto en ese momento. Las zonas más pobladas muestran más, las rurales menos, y un radio\n      más amplio ayuda mucho. Por eso la prueba es gratis por 14 días sin tarjeta:\n      revise su propia zona antes de decidir.'),
    # reviews
    ('<span class="eyebrow">From the field</span>\n      <h2>What contractors say</h2>', '<span class="eyebrow">Desde la obra</span>\n      <h2>Lo que dicen los contratistas</h2>'),
    # pricing
    ('<span class="eyebrow">Pricing</span>\n      <h2>Simple, honest pricing</h2>\n      <p>Start free for 14 days. No card to try it. Cancel anytime.</p>',
     '<span class="eyebrow">Planes</span>\n      <h2>Precios simples y honestos</h2>\n      <p>Empiece gratis por 14 días. Sin tarjeta para probarla. Cancele cuando quiera.</p>'),
    ('<span class="tag">Free trial</span>\n        <div class="amt">$0<span> / 14 days</span></div>\n        <ul><li>Full location-based scanning</li><li>AI bid extraction</li><li>Going rates &amp; target prices</li><li>Save &amp; track leads</li><li>No credit card required</li></ul>\n        <a class="btn btn-line" href="/app.html">Start free trial</a>',
     '<span class="tag">Prueba gratis</span>\n        <div class="amt">$0<span> / 14 días</span></div>\n        <ul><li>Búsqueda completa por ubicación</li><li>Lectura de licitaciones con IA</li><li>Precios de mercado y precios objetivo</li><li>Guardar y seguir trabajos</li><li>Sin tarjeta de crédito</li></ul>\n        <a class="btn btn-line" href="/app.html?lang=es">Empezar la prueba gratis</a>'),
    ('<span class="tag">Most popular</span>\n        <div class="amt">$49<span> / mo</span></div>\n        <ul><li>Unlimited nationwide scans</li><li>AI bid extraction</li><li>Going rates &amp; target prices</li><li>Bid forms filled in</li><li>Addendum &amp; due-date alerts</li><li>Ready-mix plants &amp; concrete cost</li><li>Federal + local bids</li><li>Cancel anytime</li></ul>',
     '<span class="tag">El más popular</span>\n        <div class="amt">$49<span> / mes</span></div>\n        <ul><li>Búsquedas ilimitadas en todo el país</li><li>Lectura de licitaciones con IA</li><li>Precios de mercado y precios objetivo</li><li>Formularios de licitación llenados</li><li>Avisos de adendas y fechas de entrega</li><li>Plantas de premezclado y costo del concreto</li><li>Licitaciones federales y locales</li><li>Cancele cuando quiera</li></ul>'),
    ('>Subscribe monthly</a>', '>Suscribirse mensual</a>'),
    ('<p class="renew-note">Renews automatically at $49/month until you cancel.\n           Cancel anytime from the Account screen.</p>',
     '<p class="renew-note">Se renueva automáticamente a $49/mes hasta que usted cancele.\n           Cancele cuando quiera desde la pantalla Cuenta.</p>'),
    ('<span class="tag">Annual · best value, save $189</span>\n        <div class="amt">$399<span> / yr</span></div>\n        <ul><li>Everything in Monthly</li><li>About 4 months free</li><li>Priority email support</li><li>Early access to new features</li></ul>',
     '<span class="tag">Anual · el mejor precio, ahorra $189</span>\n        <div class="amt">$399<span> / año</span></div>\n        <ul><li>Todo lo del plan mensual</li><li>Unos 4 meses gratis</li><li>Soporte prioritario por correo</li><li>Acceso anticipado a funciones nuevas</li></ul>'),
    ('>Subscribe annual</a>', '>Suscribirse anual</a>'),
    ('<p class="renew-note">Renews automatically at $399/year until you cancel.\n           Cancel anytime from the Account screen.</p>',
     '<p class="renew-note">Se renueva automáticamente a $399/año hasta que usted cancele.\n           Cancele cuando quiera desde la pantalla Cuenta.</p>'),
    # faq
    ('<div class="sec-head"><span class="eyebrow">FAQ</span><h2>Common questions</h2></div>',
     '<div class="sec-head"><span class="eyebrow">Preguntas</span><h2>Preguntas frecuentes</h2></div>'),
    ('<details><summary>What exactly does CurbCall Pro do?</summary><p>It scans government and municipal sites near you, reads their bid pages, and uses AI to pull out live sidewalk, ADA ramp, and curb &amp; gutter bids — the title, scope, deadline, and who to contact. Instead of checking dozens of city sites by hand, you get a clean feed of concrete jobs near you.</p></details>',
     '<details><summary>¿Qué hace exactamente CurbCall Pro?</summary><p>Revisa los sitios del gobierno y de los municipios cerca de usted, lee sus páginas de licitación y usa IA para sacar las licitaciones abiertas de aceras, rampas ADA y bordillo y cuneta: el título, el alcance, la fecha límite y a quién contactar. En vez de revisar a mano docenas de sitios de ciudades, recibe una lista limpia de trabajos de concreto cerca de usted. Los anuncios de las agencias están en inglés, como los publica cada agencia.</p></details>'),
    ('<details><summary>Where do the prices come from?</summary><p>From the state transportation departments\' own bid results: what contractors bid, and what won, on sidewalk, ramp and curb work. 18 states have their own prices today; in the rest of the lower 48 the app starts from the nearest of those states and says so. The numbers are rebuilt every month, and each price in the app says which state it comes from. <a href="/concrete-cost/index.html">The concrete price pages</a> show the same numbers, free.</p></details>',
     '<details><summary>¿De dónde salen los precios?</summary><p>De los resultados de licitación de los propios departamentos de transporte estatales: lo que ofrecieron los contratistas, y lo que ganó, en trabajo de aceras, rampas y bordillos. Hoy 18 estados tienen sus propios precios; en el resto de los 48 estados continentales la app empieza con los del estado más cercano y lo dice. Los números se rehacen cada mes, y cada precio en la app dice de qué estado viene. <a href="/concrete-cost/index.html">Las páginas de precios de concreto</a> muestran los mismos números, gratis (en inglés).</p></details>'),
    ("<details><summary>Does it submit bids for me?</summary><p>No. It fills in the agency's form with the details you've saved and lists what the bid asks for, so you're not starting from a blank page. You check it, sign it and submit it. It never signs, notarizes or sends a bid for you.</p></details>",
     '<details><summary>¿Entrega las licitaciones por mí?</summary><p>No. Llena el formulario de la agencia con los datos que usted guardó y le dice lo que pide la licitación, para que no empiece desde cero. Usted lo revisa, lo firma y lo entrega. Nunca firma, notariza ni envía una licitación por usted.</p></details>'),
    ('<details><summary>Do I have to install anything?</summary><p>No — CurbCall Pro runs right in your browser on phone or desktop. Add it to your home screen and it works like an app on your phone.</p></details>',
     '<details><summary>¿Tengo que instalar algo?</summary><p>No. CurbCall Pro funciona en el navegador de su teléfono o computadora. Agréguela a su pantalla de inicio y funciona como una app en su teléfono.</p></details>'),
    ('<details><summary>Do I need to know the city websites?</summary><p>No. Just enter your ZIP or let the app detect your location, pick a radius, and it finds the sites for you.</p></details>',
     '<details><summary>¿Necesito conocer los sitios web de las ciudades?</summary><p>No. Solo escriba su código postal o deje que la app detecte su ubicación, escoja un radio, y ella encuentra los sitios.</p></details>'),
    ('<details><summary>How does the free trial work?</summary><p>You get 14 days of full access with no credit card required. Find real bids first, then decide. When the trial ends, subscribe to keep going.</p></details>',
     '<details><summary>¿Cómo funciona la prueba gratis?</summary><p>Tiene 14 días de acceso completo sin tarjeta de crédito. Encuentre licitaciones reales primero y luego decida. Cuando termine la prueba, suscríbase para seguir.</p></details>'),
    ('<details><summary>How do payments work?</summary><p>Payments run securely through Stripe. Choose Monthly or Annual and complete checkout — the app unlocks automatically, and you can restore access on any device with your email. Cancel anytime.</p></details>',
     '<details><summary>¿Cómo funcionan los pagos?</summary><p>Los pagos se hacen de forma segura con Stripe. Escoja Mensual o Anual y termine el pago: la app se desbloquea sola, y puede recuperar el acceso en cualquier aparato con su correo. Cancele cuando quiera. Los Términos y la Política de privacidad están en inglés.</p></details>'),
    # band + footer
    ('<h2>Stop hunting. Start pouring.</h2>\n      <p>Find the sidewalk, ADA, and curb bids in your area this week — free for 14 days, no card.</p>\n      <a class="btn btn-amber btn-lg" href="/app.html">Open the app</a>',
     '<h2>Deje de buscar. Empiece a colar.</h2>\n      <p>Encuentre esta semana las licitaciones de aceras, ADA y bordillos en su zona, gratis por 14 días, sin tarjeta.</p>\n      <a class="btn btn-amber btn-lg" href="/app.html?lang=es">Abrir la app</a>'),
    ('<div style="margin-top:0.3rem;">Sidewalk, ADA &amp; curb bid intelligence for concrete contractors.</div>',
     '<div style="margin-top:0.3rem;">Licitaciones de aceras, ADA y bordillos para contratistas de concreto.</div>'),
    ('<div class="foot-links"><a href="#how">How it works</a><a href="#pricing">Pricing</a><a href="/support.html">Help</a><a href="/terms.html">Terms</a><a href="/privacy.html">Privacy</a><a href="/concrete-cost/index.html">Concrete prices</a><a href="/concrete-bids/index.html">Concrete bids</a><a href="/es/" lang="es">Español</a><a href="/app.html">Launch app</a></div>',
     '<div class="foot-links"><a href="#how">Cómo funciona</a><a href="#pricing">Planes</a><a href="/support.html">Ayuda</a><a href="/terms.html">Términos (inglés)</a><a href="/privacy.html">Privacidad (inglés)</a><a href="/concrete-cost/index.html">Precios de concreto</a><a href="/concrete-bids/index.html">Licitaciones de concreto</a><a href="/" lang="en">English</a><a href="/app.html?lang=es">Abrir la app</a></div>'),
    ('Questions? <a href="mailto:support@curbcallpro.com" style="color:var(--amber);text-decoration:underline;">support@curbcallpro.com</a> &mdash; a real person answers.<br />\n    © 2026 CurbCall Pro by Oblique Systems, LLC. Payments powered by Stripe.',
     '¿Preguntas? <a href="mailto:support@curbcallpro.com" style="color:var(--amber);text-decoration:underline;">support@curbcallpro.com</a>: le contesta una persona.<br />\n    © 2026 CurbCall Pro de Oblique Systems, LLC. Pagos con Stripe.'),
    # scripts: what the page says as it answers
    ('out.textContent="Enter a ZIP code or city first.";', 'out.textContent="Primero escriba un código postal o una ciudad.";'),
    ('out.textContent="Checking… (can take up to a minute if the server\'s asleep)";', 'out.textContent="Revisando… (puede tardar hasta un minuto si el servidor está dormido)";'),
    ('? "Couldn\'t find that place — try a ZIP code."\n              : "Couldn\'t check right now. Try again in a moment.";',
     '? "No encontramos ese lugar. Pruebe con un código postal."\n              : "No pudimos revisar ahora. Intente de nuevo en un momento.";'),
    ('\'<div style="margin-top:0.45rem;color:var(--text3);font-size:0.85rem;">Nearest: \'', '\'<div style="margin-top:0.45rem;color:var(--text3);font-size:0.85rem;">Las más cercanas: \''),
    ('var verdict = d.agencies>=40 ? "Strong coverage here."\n                      : d.agencies>=15 ? "Solid coverage here."\n                      : lists.length   ? "Fewer town bid pages here, but the state lists below cover the small towns."\n                      : d.agencies>0   ? "Thin coverage here — a wider radius will help."\n                                       : "No verified pages in range yet — a scan would fall back to search only.";',
     'var verdict = d.agencies>=40 ? "Muy buena cobertura aquí."\n                      : d.agencies>=15 ? "Buena cobertura aquí."\n                      : lists.length   ? "Menos páginas de pueblos aquí, pero las listas estatales de abajo cubren los pueblos chicos."\n                      : d.agencies>0   ? "Poca cobertura aquí; un radio más amplio ayudará."\n                                       : "Todavía no hay páginas verificadas en este radio; una búsqueda usaría solo el buscador.";'),
    ('\'<div style="margin-top:0.45rem;color:var(--text2);font-size:0.9rem;">Also read on every scan: \'', '\'<div style="margin-top:0.45rem;color:var(--text2);font-size:0.9rem;">También se lee en cada búsqueda (en inglés): \''),
    ("+ d.agencies + ' verified agencies</b> within ' + d.radius\n            + ' mi of ' + esc(d.location) + '. '",
     "+ d.agencies + ' agencias verificadas</b> a ' + d.radius\n            + ' mi de ' + esc(d.location) + '. '"),
    ('out.textContent="Couldn\'t reach the server. Try again in a moment.";', 'out.textContent="No pudimos conectar con el servidor. Intente de nuevo en un momento.";'),
    ('var MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];', 'var MON=["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"];'),
    ('return m ? "due " + MON[+m[2]-1] + " " + (+m[3]) : "due date on the posting";', 'return m ? "entrega " + (+m[3]) + " " + MON[+m[2]-1] : "fecha de entrega en el anuncio";'),
    ("box.innerHTML='<div class=\"nb\"><div class=\"nb-h\">We haven\\'t scanned around ' + esc(d.location) + ' lately</div>'\n              + '<div class=\"nb-note\" style=\"margin:0 0 0.7rem;\">Start free and the app reads every agency above for open sidewalk, curb and ramp bids, in about a minute.</div>'\n              + '<a class=\"btn btn-amber\" href=\"/app.html\">Start free: scan it now</a></div>';",
     "box.innerHTML='<div class=\"nb\"><div class=\"nb-h\">No hemos buscado cerca de ' + esc(d.location) + ' últimamente</div>'\n              + '<div class=\"nb-note\" style=\"margin:0 0 0.7rem;\">Empiece gratis y la app revisa cada agencia de arriba buscando licitaciones abiertas de aceras, bordillos y rampas, en más o menos un minuto.</div>'\n              + '<a class=\"btn btn-amber\" href=\"/app.html?lang=es\">Empiece gratis: busque ahora</a></div>';"),
    ('var more=d.count>d.bids.length ? "See all " + d.count + " and price them" : "Open them and price them";',
     'var more=d.count>d.bids.length ? "vea las " + d.count + " y póngales precio" : "ábralas y póngales precio";'),
    ("box.innerHTML='<div class=\"nb\"><div class=\"nb-h\">Open right now near ' + esc(d.location) + '</div><ul>'",
     "box.innerHTML='<div class=\"nb\"><div class=\"nb-h\">Abiertas ahora cerca de ' + esc(d.location) + ' (anuncios en inglés)</div><ul>'"),
    ("+ '</ul><a class=\"btn btn-amber\" href=\"/app.html\">Start free: ' + esc(more) + '</a>'\n            + '<div class=\"nb-note\">Free for 14 days, no card. The app reads each one\\'s bid form and gives you a target price.</div></div>';",
     "+ '</ul><a class=\"btn btn-amber\" href=\"/app.html?lang=es\">Empiece gratis: ' + esc(more) + '</a>'\n            + '<div class=\"nb-note\">Gratis por 14 días, sin tarjeta. La app lee el formulario de cada una y le da un precio objetivo.</div></div>';"),
    ("el.innerHTML='<b><span class=\"dot\"></span>'+t+' of public work won</b><span>· reported by CurbCall customers, '+d.wins+' jobs</span>';",
     "el.innerHTML='<b><span class=\"dot\"></span>'+t+' en obra pública ganada</b><span>· reportado por clientes de CurbCall, '+d.wins+' trabajos</span>';"),
    ('nWord.textContent=(v===1?"bid":"bids");', 'nWord.textContent=(v===1?"licitación":"licitaciones");'),
    ('{x:104,y:78, t:"ADA Ramp Upgrades",     w:"Springfield, MO",d:"Due Jul 9"},\n    {x:226,y:112,t:"Sidewalk Replacement",  w:"Nixa, MO",       d:"$310k"},\n    {x:196,y:170,t:"Curb & Gutter — 5th",   w:"Ozark, MO",      d:"Due Jul 22"},\n    {x:88, y:152,t:"Concrete Street Repair",w:"Republic, MO",   d:"Due Aug 3"},\n    {x:250,y:58, t:"Trail Paving Phase 2",  w:"Willard, MO",    d:"Due Aug 14"}];',
     '{x:104,y:78, t:"Mejoras de rampas ADA",  w:"Springfield, MO",d:"Entrega 9 jul"},\n    {x:226,y:112,t:"Reemplazo de aceras",    w:"Nixa, MO",       d:"$310k"},\n    {x:196,y:170,t:"Bordillo y cuneta, 5th", w:"Ozark, MO",      d:"Entrega 22 jul"},\n    {x:88, y:152,t:"Reparación de calle",    w:"Republic, MO",   d:"Entrega 3 ago"},\n    {x:250,y:58, t:"Sendero, fase 2",        w:"Willard, MO",    d:"Entrega 14 ago"}];'),
]


def build():
    with open(SRC, encoding="utf-8") as f:
        page = f.read()
    missing = []
    for en, es in PAIRS:
        if en not in page:
            missing.append(en[:90])
            continue
        page = page.replace(en, es)
    if missing:
        raise SystemExit("index.html changed; update tools/build_es_landing.py for:\n  " + "\n  ".join(missing))
    return page


def main():
    page = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
