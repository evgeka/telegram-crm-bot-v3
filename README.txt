1. У Railway Variables мають бути:
BOT_TOKEN=ваш_новий_токен
DB_PATH=/data/crm.sqlite

2. У Railway має бути Volume, змонтований в /data

3. Найпростіший спосіб оновлення:
- створити НОВИЙ GitHub репозиторій
- завантажити туди всі файли з цієї папки
- підключити цей репозиторій до Railway через Connect Repo

4. Якщо хочете залишити старий репозиторій:
- відкрийте bot.py і повністю замініть його вміст
- requirements.txt теж замініть
- railway.json додайте
- закомітьте зміни

5. Після deploy у Telegram натисніть /start
