# Gemini + מעקב ערוצי יוטיוב

בכתובת אחת, `my-gemini.<שם>.workers.dev`, יש שתי מערכות, ומעבר ביניהן בלחיצת כפתור:
- **`/`**: הצ'אט עם Gemini, בדיוק כמו קודם.
- **`/yt/`**: מעקב ערוצים. כשעולה סרטון חדש, הוא יורד אוטומטית בשרת, ומשם ישר למחשב.

```
Cloudflare Worker (my-gemini)            Render (שרת ההורדות)
 ├─ /     Gemini                           Dockerfile → agent/agent.py
 ├─ /yt   דף + RSS כל 3 שעות  ──תור + "התעורר"──▶  yt-dlp (עוגיות + Node)
 └─ /yt/api               ◀──────דיווח───────  /files/… (זמני)
          הדף פתוח ← הורדה אוטומטית למחשב ◀── הקובץ
```

## חלק 1: ה-Worker הקיים (`my-gemini`)

1. **הדבקת הקוד.** פותחים את `my-gemini` ולוחצים **Edit code**. מוחקים הכל, מדביקים את כל `worker/worker.js` ולוחצים **Deploy**.
2. **סודות.** תחת **Settings ← Variables and Secrets ← Add**, בוחרים **Type: Secret** ומוסיפים:
   - `GEMINI_API_KEY`: מפתח ה-API של Gemini. **מפתח חדש**, לא זה שהיה כתוב בקוד הישן.
   - `GEMINI_PASSWORD`: משפט הכניסה לצ'אט.
   - `ADMIN_PASSWORD`: הסיסמה לדף הערוצים.
   - `AGENT_TOKEN`: מחרוזת ארוכה ואקראית. אותה מחרוזת תיכנס גם ל-Render.
3. **משתנה רגיל (Type: Text).** `RENDER_URL` = הכתובת של שרת Render, למשל `https://yt-agent.onrender.com`.
4. **מסד נתונים (KV).**
   1. בתפריט: **Storage & Databases ← KV ← Create**, בשם `yt-tracker`.
   2. ב-Worker: **Settings ← Bindings ← Add ← KV namespace**. Variable name: `DB`.
5. **בדיקה אוטומטית.** ב-**Settings ← Trigger Events ← Add ← Cron Triggers** מזינים `0 */3 * * *`.

## חלק 2: Render (שרת ההורדות)

1. **יצירת השירות.** ב-Render לוחצים **New ← Web Service** ומחברים את הריפו הזה. **Language: Docker**, ו-**Instance type: Free**.
2. **Environment.**
   - `TRACKER_URL` = `https://my-gemini.<שם>.workers.dev/yt`
   - `AGENT_TOKEN` = אותה מחרוזת שהכנסת ב-Worker.
   - `YTDLP_COOKIES` = `/etc/secrets/cookies.txt`
3. **עוגיות.** תחת **Secret Files**, יוצרים קובץ בשם `cookies.txt` ומדביקים בו את העוגיות של youtube.com (בפורמט Netscape, עם התוסף "Get cookies.txt LOCALLY"). **לעולם לא בצ'אט ולא בריפו.**
4. **Health Check Path:** `/healthz`. לוחצים **Create**.

## שימוש

- **מעבר בין המערכות:** בצ'אט יש כפתור **📺 ערוצים**, ובדף הערוצים יש כפתור **💬 מעבר ל-Gemini**.
- **שרת ההורדות:** Render נרדם אחרי כ-15 דקות בלי שימוש. ה-Worker מעיר אותו לבד בכל בדיקה ובכל פעם שמשהו נכנס לתור. אפשר להעיר אותו גם ידנית עם **⏰ הער את שרת ההורדות**. ההתעוררות לוקחת עד דקה.
- **הורדה אוטומטית למחשב:** מסמנים את התיבה ומשאירים את הדף פתוח. כל קובץ שמסיים לרדת בשרת יורד לבד למחשב. בפעם הראשונה הדפדפן ישאל אם לאשר הורדה של כמה קבצים, ועונים כן.
- **הקבצים ב-Render זמניים.** הם נמחקים כשהשרת נרדם, או אחרי 6 שעות. אם יש לך R2, אפשר להוסיף את הפרטים שלו ב-Render (ראה `agent/agent.env.example`), ואז הקבצים נשמרים שם קבוע.

---

## אפשרות אחרת: שרת Linux רגיל במקום Render


**שרת חינמי:** אפשר לפתוח שרת ב-Oracle Cloud, תחת **Always Free**, עם מכונת VM של Ubuntu. ההרשמה מבקשת כרטיס אשראי לאימות בלבד.

```bash
git clone <הריפו הזה> && cd */agent
sudo bash install.sh
sudo nano /opt/yt-agent/agent.env      # TRACKER_URL, AGENT_TOKEN (ו-R2 אם יש)
sudo nano /opt/yt-agent/cookies.txt    # עוגיות youtube.com (Netscape) — מומלץ מאוד
sudo systemctl restart yt-agent
journalctl -u yt-agent -f              # לראות מה קורה
```

`install.sh` מתקין:
- **הכלים להורדה:** Python, ffmpeg ו-Node אמיתי, וגם `yt-dlp[default]`, ‏`curl_cffi` ו-PO Token sidecar ב-Docker.
- **שירות שרץ תמיד** וחוזר לבד אחרי הפעלה מחדש.
- **עדכון יומי של yt-dlp.**

**עוגיות:** בשרת של מרכז נתונים יוטיוב יבקש "Sign in to confirm you're not a bot" אם אין עוגיות.
- מייצאים רק את `youtube.com`, עם התוסף "Get cookies.txt LOCALLY", כשאתה מחובר.
- אף פעם לא שומרים אותן בריפו. `.gitignore` כבר חוסם את הקובץ.
- כשהעוגיות פגות, מייצאים חדשות ומחליפים את הקובץ בשרת.

**איפה הקבצים:**
- **עם R2:** כפתור "הורדה" בדף.
- **בלי R2:** הקבצים נשמרים בשרת, בתיקייה `/opt/yt-agent/downloads`, והדף מציג את הנתיב.

## הגבלות

- **הגבלת גיל:** `AGE_LIMIT=17` בסוכן מדלג על תוכן שהאתר מסמן כ-18+.
- **רק יוטיוב, ורק לשימוש אישי.** כדאי להוריד רק תוכן שמותר לך להוריד.
- **ה-RSS של יוטיוב מחזיר רק את 15 הסרטונים האחרונים של ערוץ.** הבדיקה כל 3 שעות מספיקה בשביל כמעט כל ערוץ.
- **מכסות חינם ב-KV:** 1,000 כתיבות ביום. זה מספיק לעשרות ערוצים.
