# Pase de Lista

Prototipo académico de asistencia por sesión mediante QR escaneado por el docente, con roles, varios planteles, grupos, historial y exportación CSV. No solicita ni usa la ubicación del teléfono. Está pensado para mostrar el flujo y recibir comentarios; **no es una aplicación lista para tomar asistencia oficial ni guardar datos reales**.

## Ejecutar en Windows

Abre PowerShell y pega lo siguiente. Mantén esa ventana abierta mientras usas la aplicación:

```powershell
Set-Location 'F:\Pase De Lista'
$env:PASELISTA_HOST='0.0.0.0'
$env:PASELISTA_PORT='8000'
$env:PASELISTA_TIMEZONE='America/Mexico_City'
$env:PASELISTA_DEMO_MODE='1'
$env:PASELISTA_ADMIN_EMAIL='haressanchez506@gmail.com'
$env:PASELISTA_ADMIN_PASSWORD='define-una-clave-segura-de-al-menos-12-caracteres'
& '.\.venv\Scripts\python.exe' .\app.py
```

`PASELISTA_HOST='0.0.0.0'` permite que otros dispositivos de tu red local alcancen el servidor. Mantén esta ventana abierta. En otra ventana de PowerShell, encuentra la dirección IPv4 de la computadora:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Format-Table InterfaceAlias,IPAddress
```

En el celular conectado a la misma red Wi-Fi, abre exactamente `http://DIRECCION-IP-DE-LA-PC:8000` (por ejemplo, `http://192.168.1.25:8000`), con `http://`, no `https://`. Este servidor de demostración no tiene certificado ni acepta conexiones HTTPS: al usar `https://` el navegador envía datos TLS al puerto HTTP y el servidor no puede abrir la página. No uses `127.0.0.1` ni `localhost` en el celular: apuntan al propio teléfono. Si Windows pregunta, permite Python sólo en redes privadas; no habilites redes públicas. Si todavía rechaza la conexión, abre PowerShell **como administrador** y crea una regla limitada al puerto de la demo, redes privadas y dispositivos de la subred local:

```powershell
New-NetFirewallRule -DisplayName 'Pase de Lista demo (red privada)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Private -RemoteAddress LocalSubnet
```

Verifica también que ambos dispositivos estén en la misma Wi-Fi y que la red no tenga aislamiento de clientes.

La cuenta administradora usa `PASELISTA_ADMIN_EMAIL` (por defecto `haressanchez506@gmail.com`). Define `PASELISTA_ADMIN_PASSWORD` antes del primer inicio; la aplicación guarda sólo un hash de la contraseña. Si la cuenta ya fue creada, cambiar esa variable no reemplaza la contraseña guardada. No uses una contraseña que hayas compartido en mensajes o código. Esta conexión HTTP local permite abrir y revisar la página, pero los navegadores normalmente requieren HTTPS para permitir que el sitio use la cámara del celular. La app no solicita ubicación. No uses esta demo por Wi-Fi público, no la expongas a Internet ni guardes datos reales.

## Recorrido para presentar

1. Inicia sesión como **Administración**. Revisa el plantel y explora la creación de planteles y grupos.
2. Inscribe al estudiante de demostración en un grupo desde el panel de administración.
3. En administración, registra al estudiante con su correo institucional, matrícula y teléfono del tutor; después inscríbelo en un grupo.
4. Cierra sesión e inicia como **Docente**. Abre el pase de su grupo, toca «Escanear QR» y permite el acceso a la cámara. El QR del estudiante registra la asistencia; detén el pase cuando termines.
5. Vuelve al perfil de docente o administración para consultar los registros y descargar el CSV.

La base SQLite se crea como `pase_lista.sqlite3`. El esquema ya separa escuelas, grupos, inscripciones, sesiones de clase y asistencias; se puede guardar más de una asistencia por alumno en el mismo día si corresponde a clases distintas. Al actualizar una base de la versión anterior, las asistencias diarias existentes se importan una sola vez como sesiones históricas. Los registros guardan fecha/hora UTC y no conservan las coordenadas del estudiante. Cada PIN tiene seis dígitos, vence en 60 segundos por defecto y limita a cinco intentos por estudiante y clase. El docente puede emitir otro PIN en cualquier momento. El pase permanece abierto hasta que el docente lo detenga. La zona horaria se guarda como dato del plantel, pero esta demo todavía no calcula horarios de clase; el navegador presenta horas en su propia zona.

## Credenciales QR y uso desde celular

Cada estudiante tiene una credencial QR propia. El código sólo contiene el prefijo de formato `PL1:` y un token aleatorio de 256 bits; **no incluye nombre, matrícula, correo ni teléfono**. La base guarda el hash SHA-256 del token, no el token original. Si se pierde o se comparte la credencial, administración puede generar otra: el QR anterior queda revocado.

El flujo de asistencia es: el profesor inicia sesión, abre el pase de su grupo y escanea la credencial con la cámara del teléfono. El servidor comprueba que el profesor tenga asignado el grupo, que la clase siga abierta, que el estudiante esté inscrito y que aún no tenga asistencia en esa sesión. Después guarda el registro en la base y, si está configurado Twilio, intenta enviar el aviso al tutor. Un QR es una credencial secreta —quien tenga una copia puede intentar usarla—, por eso hay que proteger las credenciales impresas y renovar inmediatamente cualquier QR extraviado. La asistencia por QR no demuestra por sí sola quién sostiene la credencial.

La aplicación web ya incluye el escáner y puede instalarse como **PWA** (acceso directo con apariencia de aplicación, sin pasar por una tienda). La pantalla de acceso explica cómo instalarla: en Android, abrir el enlace en Chrome y elegir «Instalar app» o «Agregar a pantalla principal»; en iPhone/iPad, abrirlo en Safari, tocar «Compartir» y «Agregar a pantalla de inicio». Para instalarla como PWA y usar la cámara, el servidor debe estar publicado en una dirección accesible por el dispositivo y con **HTTPS**. `localhost` sólo funciona en el dispositivo donde corre el servidor; el acceso por HTTP a la IP de la PC puede mostrar la página, pero el navegador normalmente bloquea la cámara. La demo usa HTTP y SQLite local, así que no debe usarse con datos reales. Antes de producción hacen falta dominio y TLS, autenticación institucional, servidor de producción, base compartida (por ejemplo PostgreSQL), respaldos y pruebas de restauración.

## Configuración

| Variable | Uso |
| --- | --- |
| `PASELISTA_HOST` | Dirección de escucha; por defecto `127.0.0.1` (sólo este equipo). |
| `PASELISTA_PORT` | Puerto HTTP; por defecto `8000`. |
| `PASELISTA_DB` | Ruta alternativa a la base SQLite. |
| `PASELISTA_TIMEZONE` | Zona IANA del plantel inicial, por defecto `America/Mexico_City`. |
| `PASELISTA_TOKEN_TTL_SECONDS` | Vida del PIN, por defecto `60`. |
| `PASELISTA_DEMO_MODE` | Habilita las cuentas ficticias de muestra; por defecto `1`. |
| `PASELISTA_ADMIN_EMAIL` | Correo administrador; por defecto `haressanchez506@gmail.com`. |
| `PASELISTA_ADMIN_PASSWORD` | Contraseña inicial del administrador, aplicada sólo al crear la cuenta. Configúrala como secreto. |
| `PASELISTA_SMTP_USER`, `PASELISTA_SMTP_PASSWORD` | Usuario Gmail y contraseña de aplicación SMTP; necesarios para recuperar contraseñas. No uses la contraseña normal de Google. |
| `PASELISTA_SMTP_FROM` | Dirección que aparecerá como remitente; opcional, usa el usuario SMTP por defecto. |
| `PASELISTA_SMTP_HOST`, `PASELISTA_SMTP_PORT` | Servidor y puerto SMTP; por defecto `smtp.gmail.com` y `587`. |
| `PASELISTA_SECRET` | Secreto estable para los PINes entre reinicios. Debe ser aleatorio, de al menos 32 bytes y estar fuera del código. |
| `PASELISTA_COOKIE_SECURE` | Usa `1` para marcar cookies `Secure` detrás de HTTPS. |
| `PASELISTA_TWILIO_ACCOUNT_SID`, `PASELISTA_TWILIO_AUTH_TOKEN` | Credenciales del proveedor Twilio para enviar avisos SMS. |
| `PASELISTA_TWILIO_FROM_NUMBER` | Número emisor de Twilio en formato internacional. |

El plantel y los grupos también pueden administrarse desde la pantalla. Las bases de datos existentes conservan las columnas antiguas de geocerca por compatibilidad, pero la aplicación ya no las usa y borra las coordenadas guardadas al iniciar la nueva versión.

## Cuentas y recuperación

La cuenta administradora se crea al inicializar la base. Desde el menú de administración se pueden crear cuentas docentes con correo y contraseña individual (mínimo 12 caracteres). El inicio de sesión verifica contraseñas con PBKDF2; los códigos de recuperación de seis dígitos se envían por correo, vencen en 10 minutos y permiten cinco intentos. Para Gmail, configura una contraseña de aplicación SMTP en `PASELISTA_SMTP_PASSWORD`. Sin SMTP configurado, el inicio con contraseña funciona, pero la recuperación por correo no.

## Alcance pendiente antes de producción

- **Identidad y operación:** para producción, considera inicio institucional (Microsoft Entra ID o Google OIDC), MFA, políticas de credenciales, verificación de dominio y protección contra abuso de recuperación. La autenticación de correo y contraseña incluida aquí no convierte por sí sola el prototipo en un servicio institucional listo para producción.
- **Despliegue:** usar un servidor WSGI/ASGI de producción y un proxy HTTPS con certificado, dominio, monitoreo, copias de seguridad y restauración probada. El servidor de desarrollo escucha en `127.0.0.1` por defecto; `0.0.0.0` lo hace accesible desde otras interfaces para la demo en una red privada, pero no lo expongas a Internet, redes públicas ni a la red escolar. La cámara requiere HTTPS al acceder desde otro dispositivo.
- **Operación multiinstancia:** compartir base de datos, secretos y almacenamiento de sesión entre procesos; planificar migraciones, concurrencia, retención y respaldos. SQLite y este servidor son para uso local/de demostración.
- **Autorización escolar:** definir quién puede administrar cada plantel, asignar docentes y alumnos desde el sistema institucional, y configurar calendario/horarios y excepciones.
- **Privacidad y antifraude:** definir avisos, consentimiento o base jurídica, acceso y retención de datos. Un QR es una credencial que puede copiarse o compartirse; proteger las credenciales impresas y revocar inmediatamente cualquier QR extraviado.
- **Avisos al tutor:** configura las tres variables de Twilio para activar SMS. Sin ellas, la asistencia se registra normalmente y la pantalla indica que no se pudo enviar el aviso. Confirma el consentimiento para usar el teléfono del tutor y considera costos y límites del proveedor.
- **Pruebas de aceptación:** probar navegadores y teléfonos reales, escaneo QR, zonas horarias, clases simultáneas, fallos de red, reinicio del servidor, restauración de respaldos y exportación.

El PIN activo usa memoria del servidor y se invalida al reiniciar si no se configura `PASELISTA_SECRET`. Se guardan sesiones autenticadas y configuración en SQLite.

## Pruebas

```powershell
& '.\.venv\Scripts\python.exe' -m unittest -v
```
