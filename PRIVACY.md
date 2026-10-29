# Privacy Policy & Community Code of Conduct — Anonymous AUA Chat Bot

**Effective Date:** October 2026

Welcome to the **Anonymous AUA Chat Bot** ("the Bot"). This is an independent, student-developed project designed for the American University of Armenia (AUA) community and is not officially affiliated with AUA administration.

---

## 1. How Your Anonymity & Data Are Protected

### 1.1 Data Stored in Our Database (PostgreSQL)
To match compatible partners and maintain community quality, the Bot stores only:
- **Telegram User ID:** Used strictly to route messages and maintain account state. Your ID, name, and `@username` are **never** shown to your chat partner.
- **Matchmaking Profile:** Your selected Sex (`Male` / `Female`) and Orientation (`Straight` / `Gay` / `Lesbian`).
- **Priority Score (0–100):** Starts at `50` and adjusts automatically based on post-chat feedback (`Like` +1, `Dislike` -1, `Spam` -5, `Not from AUA` -5) and AI Moderation verdicts.
- **Moderation Metadata:** Aggregate counters of feedback and anonymized AI report verdicts.

### 1.2 Zero-Knowledge Ephemeral Chat Buffer (No Permanent Chat Logs)
- **Human administrators CANNOT read your conversations.** Messages and photos are never written to any disk or database.
- **Bounded 5-Minute RAM Buffer:** During an active chat, a rolling window of the most recent **60 messages** (max 500 characters each) and the **8 most recent compressed images** is held temporarily in volatile server memory (RAM) under anonymous aliases (`User_A` and `User_B`).
- **Automatic Destruction:** If no report is filed within **5 minutes** after a chat ends (`/stop` or `/next`), the memory buffer is permanently erased.
- **Automated AI Review:** If a user clicks **Report**, the ephemeral memory buffer is sent once to the **Google Gemini API** (`gemini-2.5-flash`) for automated multimodal safety evaluation (including OCR on images) and is immediately wiped from server RAM the millisecond the API call completes.

---

## 2. Community Code of Conduct & Mutual AI Judging Rules

When a report is submitted, the AI Moderator evaluates the **entire conversation context** and judges **BOTH** the `Reported_User` and the `Reporter`. Either or both participants may be penalized (`-1` to `-10` priority points, temporary/permanent ban, or identity unmasking for illegal acts).

### Rule A: Privacy Violations & Anti-Doxxing (Strictly Enforced on Both Parties)
1. **Non-Consensual Identity Exposure (Doxxing):** Sharing, guessing, or threatening to expose another student's real full name, phone number, residential address, student ID, social media handles, or private photos without explicit consent is a severe violation (**Penalty: -6 to -10 priority + Block**).
2. **Coercion & Stalking:** Repeatedly pressuring, blackmailing, or harassing a partner to reveal their identity after they have declined (**Penalty: -5 to -10 priority**).
3. **Outing & Blackmail:** Threatening to screenshot or expose someone's sexual orientation or private statements to the university or third parties (**Penalty: -10 priority + Immediate Ban + Admin Escalation**).

### Rule B: Harassment, NSFW, and Abuse
1. **Unsolicited Explicit Media (Cyber-Flashing):** Sending sexually explicit images, nudity, or graphic violence without mutual consent (**Penalty: -8 to -10 priority + Block**).
2. **Hate Speech & Severe Bullying:** Targeted degradation, slurs, or sustained personal attacks (**Penalty: -5 to -10 priority**).
3. **Spam, Trolling & Off-Campus Promotion:** Commercial links, crypto scams, flood messages, or rude behavior (**Penalty: -1 to -5 priority**).

### Rule C: Reporter Accountability (Baiting, Mutual Toxicity & False Reports)
Filing a report does **not** grant immunity. The AI Moderator inspects the Reporter's own messages:
1. **Reporter Privacy/Harassment Violation:** If the Reporter themselves doxxed, harassed, or sent prohibited content to the partner, the **Reporter** will receive the full priority penalty (`-1` to `-10`) and potential ban.
2. **Provocation & Baiting:** If the Reporter intentionally provoked or insulted the partner to bait an angry reaction and then reported them, **both** users (or solely the Reporter) will be penalized.
3. **Frivolous / Malicious False Reports:** Reporting an innocent partner out of spite (e.g., because they politely ended the chat or weren't interested) results in a **-2 to -5 priority penalty applied to the Reporter**.

### Rule D: Illegal Content & Emergency Identity Unmasking
Anonymity is forfeited **only** when the AI detects **Tier 3 Illegal / Extreme Violations**:
- Child Sexual Abuse Material (CSAM) or exploitation.
- Credible threats of physical violence, terrorism, or self-harm encouragement.
- Extortion, blackmail, or non-consensual distribution of intimate images (revenge porn).
In these extreme cases, the perpetrator is immediately banned (`priority = 0`) and their Telegram User ID, `@username`, and violation summary are disclosed to the Bot Administrators for campus safety or law enforcement referral.

---

## 3. Data Deletion & User Rights
You may permanently delete your profile, preferences, and priority record at any time by using the `/delete_me` command or contacting the Bot Administrator.

