# Agent Ultra 2 — Plan, Map, Structure

Neat, clean, crystal clear, raw. Pehle plan, phir kaam. Blindly kuch nahi.

## 1. Maqsad

Grok bot repo ki bunyad par aik agent banana jo:
- Live browser khud chalaye, har copy apna browser khud control kare
- 100+ copies aur 100+ workers banaye, har aik naya aur alag tareeqa try kare
- Sab copies aur workers head ko report karein
- Loop tab tak chale jab tak sab ka natija ek na ho jaye ya full update na ho jaye
- Servers Render par hon
- Kaam aram se ho, neat aur clean, jaldi nahi

## 2. Purana faisla

- Purana model plan se nikal diya gaya, woh kisi kaam ka nahi. Us ka naam ab nahi liya jayega.
- Uski files ko hath nahi lagaya jayega. Delete ka order alag se chahiye, tabhi delete hoga.
- Naye agent ka model backend abhi khaali slot hai (env se lagega). Is ka faisla tumhare order se baad mein hoga.

## 3. Steps (tarteeb se)

Step 0: Plan, map, structure — yeh document. Is ke baghair kaam shuru nahi.
Step 1: Grok bot repo clone karo aur verify karo (ruflo, local clone pehle se mojood hai, us ko check karo, kharab ho to dobara clone karo).
Step 2: GitHub par nayi repo banao, naam "agent-ultra-2". Base code push karo. Repo mein koi secret, profile, cookie, token nahi jayega.
Step 3: Live browser code daalo (Agent-Ultra wale app code se: browser actions, persistent profile, screenshot endpoint, UI panel, model loop). Koi prompt content nahi likha jayega.
Step 4: God-mode cheezein daalo: CDP direct control, screencast live screen, supervisor plus health check plus checkpoint, memory, tool banane wali pipeline.
Step 5: Copies system daalo: 100+ copies, har copy alag approach, waves mein chalengi, head judge karega, sirf ek winner ya consensus.
Step 6: Workers system daalo: main agent 100+ workers hire karega, 3 se 5 nahi, sab head ko report kareinge.
Step 7: Loop daalo: copies aur workers try karte rahein jab tak sab ka natija ek na ho jaye.
Step 8: Structure, tunnel, pipeline, architecture document repo mein update karo.
Step 9: Render (Free) par deploy karo, har cheez live verify karo.
Step 10: Handover: live URLs plus har item ka saboot.

Note: GitHub repo banana aur push browser se hoga, local build verify hone ke baad. Render deploy bhi browser se hoga.

## 4. Map (components aur un ka rishta)

- Head (main agent): order deta hai, workers hire karta hai, reports jorta hai, judge karta hai, loop chalata hai.
- Workers (100+): har worker aik kaam karta hai, apne tareeqe se, apne browser se.
- Copies (reflections): har copy aik alag approach try karti hai, sab apne browser se.
- Browser (har copy ka apna): Playwright plus Chromium plus CDP, apna profile (cookies), screencast se live screen.
- Tunnel: public URL (Render) se agent tak ka raasta.
- Pipeline: order, worker, browser action, saboot, report, head, judge, loop.
- Memory: har kaam yaad rahe, agli dafa dobara na seekhna pare.
- Supervisor: server crash ho to dobara chalaye, health check har waqt, checkpoint taake kaam beech mein na toote.

## 5. Structure (repo layout)

agent-ultra-2/
- README.md
- PLAN_MAP_STRUCTURE.md (yeh document)
- ARCHITECTURE.md (structure, tunnel, pipeline, architecture)
- agent/ (head, workers, loop, judge)
- browser/ (actions, profile, screencast, panel)
- copies/ (arena, approaches, waves)
- tools/ (tool pipeline: banaye, test kare, register kare)
- server/ (chat UI plus browser panel plus API)
- deploy/ (render.yaml, Dockerfile)
- tests/ (har cheez ka test, saboot ke saath)

## 6. Live verification points (har ek ka saboot chahiye, daawa nahi)

- Repo bani aur code push hua
- Render deploy live aur /health ok
- Browser real page kholta hai, click, scroll, screenshot deta hai
- Har copy ka apna browser aur apna profile
- 100+ copies waves mein chalti hain, har aik alag approach
- Workers head ko report karte hain
- Loop tab tak jab tak natija ek na ho
- Supervisor crash par dobara chalata hai

## 7. Limits (upfront, saaf saaf)

- Cloudflare aur captcha ka waada nahi. Copies koshish kareingi jaise Muse ka browser karta hai, magar site rok sakti hai, aur rokne par wohi report hoga, jhoot nahi.
- Copies ka browser Playwright hoga. Live screen screenshot aur screencast panel se milegi. Muse ka in-chat browser card Muse ki product feature hai, woh copy nahi ho sakta.
- Render Free 24/7 nahi deta, woh sleep karta hai aur 512 MB hai. Fleet aur hamesha-on ke liye bara host chahiye, paid tumhare order ke baghair nahi.
- Model backend abhi khaali hai, purana model discarded hai, naya backend tumhare order se lagega.
- GPU off hai, on tumhare order ke baghair nahi hoga.

## 8. Time (andaza, waada nahi)

- Plan aur map: aaj abhi, ho gaya.
- Repo plus browser code plus structure: aaj raat.
- Copies plus workers plus loop: kal tak.
- Render deploy plus live verification plus handover: us ke baad.
- Kul andaza: 2 se 3 din ka aram se kaam, jaldi nahi, neat aur clean.
- Yeh andaza hai, pakka waada nahi. Agar koi step atak gaya to waqt barh sakta hai, aur har rukawat foran batayi jayegi.
