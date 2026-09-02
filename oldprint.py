import asyncio
from dotenv import load_dotenv
load_dotenv()
from livekit.agents import ChatContext, utils
import agent, stack

QS = [
 "పాత ప్రింట్ కాపీ పంపగలరా?",
 "గత నెల మ్యాగజైన్ ప్రింట్‌లో కావాలి",
 "జనవరి సంచిక ప్రింట్ కాపీ ఉందా?",
 "పాత ఎడిషన్లు పోస్ట్‌లో పంపిస్తారా?",
 "బ్యాక్ ఇష్యూస్ ప్రింట్‌లో దొరుకుతాయా?",
 "పాత మ్యాగజైన్లు దొరుకుతాయా?",
]

async def main():
    a = agent.GundusoodiAgent(); llm = stack.build_llm(); tools = list(a.tools)
    hc = utils.http_context; hc._ContextVar.set(hc._new_session_ctx())
    out=[]
    for q in QS:
        ctx = ChatContext(); ctx.add_message(role="system", content=a.instructions)
        ctx.add_message(role="user", content=q)
        got=""
        async for ch in llm.chat(chat_ctx=ctx, tools=tools):
            d=ch.delta
            if d and d.content: got += d.content
        out.append(f"CALLER: {q}\nBOT   : {' '.join(got.split())}\n")
    await hc._close_http_ctx()
    open('oldprint_out.txt','w',encoding='utf-8').write("\n".join(out))

asyncio.run(main())
