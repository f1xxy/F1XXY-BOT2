import os, re, sqlite3, time, unicodedata, asyncio, json, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "royal.sqlite3")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS settings (
 guild_id INTEGER PRIMARY KEY, log_channel INTEGER, quarantine_role INTEGER,
 quarantine_channel INTEGER, raid_enabled INTEGER DEFAULT 0, raid_threshold INTEGER DEFAULT 8,
 raid_window INTEGER DEFAULT 20, insult_filter INTEGER DEFAULT 1,
 ticket_panel_channel INTEGER, ticket_category INTEGER, ticket_staff_role INTEGER,
 youtube_channel_id TEXT, youtube_feed_channel INTEGER, youtube_template TEXT,
 youtube_last_video TEXT, member_counter_channel INTEGER, subscriber_counter_channel INTEGER
)""")
# Upgrade databases created by the earlier starter version.
cols = {r[1] for r in db.execute("PRAGMA table_info(settings)")}
for name, typ in [
 ("ticket_panel_channel","INTEGER"),("ticket_category","INTEGER"),("ticket_staff_role","INTEGER"),
 ("youtube_channel_id","TEXT"),("youtube_feed_channel","INTEGER"),
 ("youtube_template","TEXT"),("youtube_last_video","TEXT"),
 ("member_counter_channel","INTEGER"),("subscriber_counter_channel","INTEGER")
]:
    if name not in cols:
        db.execute(f"ALTER TABLE settings ADD COLUMN {name} {typ}")
db.execute("""CREATE TABLE IF NOT EXISTS warnings (
 guild_id INTEGER, user_id INTEGER, moderator_id INTEGER, reason TEXT, created REAL
)""")
db.execute("""CREATE TABLE IF NOT EXISTS custom_terms (guild_id INTEGER, term TEXT, UNIQUE(guild_id,term))""")
db.execute("""CREATE TABLE IF NOT EXISTS open_tickets (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER)""")
db.commit()
joins = {}
DEFAULT_TERMS = {"connard","connasse","con","conne","fdp","ntm","nique ta mere","pute","putain","encule","enculé","enculer","salope","tg","batard","bâtard","merde"}

def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", s)).strip()
def ensure(gid):
    db.execute("INSERT OR IGNORE INTO settings(guild_id) VALUES(?)", (gid,)); db.commit()
def val(gid, field, default=None):
    r = db.execute(f"SELECT {field} FROM settings WHERE guild_id=?", (gid,)).fetchone()
    return r[0] if r else default
def warn_count(gid, uid):
    return db.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?", (gid,uid)).fetchone()[0]
def terms(gid):
    return DEFAULT_TERMS | {r[0] for r in db.execute("SELECT term FROM custom_terms WHERE guild_id=?", (gid,))}
async def log(guild, msg):
    cid = val(guild.id, "log_channel")
    channel = guild.get_channel(cid) if cid else None
    if channel:
        try: await channel.send(msg)
        except discord.HTTPException: pass

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Fermer le ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="royal:ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = db.execute("SELECT guild_id,user_id FROM open_tickets WHERE channel_id=?", (interaction.channel_id,)).fetchone()
        if not row:
            return await interaction.response.send_message("Ticket non reconnu.", ephemeral=True)
        guild_id, opener_id = row
        staff_role_id = val(guild_id, "ticket_staff_role")
        is_staff = interaction.user.guild_permissions.manage_channels or (
            staff_role_id and any(r.id == staff_role_id for r in getattr(interaction.user,"roles",[])))
        if interaction.user.id != opener_id and not is_staff:
            return await interaction.response.send_message("Seul l'auteur du ticket ou le staff peut le fermer.", ephemeral=True)
        await interaction.response.send_message("Ticket fermé. Suppression dans quelques secondes.")
        await asyncio.sleep(4)
        db.execute("DELETE FROM open_tickets WHERE channel_id=?", (interaction.channel_id,)); db.commit()
        try: await interaction.channel.delete(reason=f"Ticket fermé par {interaction.user}")
        except discord.HTTPException: pass

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Créer un ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="royal:ticket_open")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Utilise ce bouton sur le serveur.", ephemeral=True)
        existing = db.execute("SELECT channel_id FROM open_tickets WHERE guild_id=? AND user_id=?",
                              (guild.id, interaction.user.id)).fetchone()
        if existing and guild.get_channel(existing[0]):
            return await interaction.response.send_message(f"Tu as déjà un ticket : {guild.get_channel(existing[0]).mention}", ephemeral=True)
        cat_id = val(guild.id, "ticket_category")
        category = guild.get_channel(cat_id) if cat_id else None
        staff_id = val(guild.id, "ticket_staff_role")
        staff_role = guild.get_role(staff_id) if staff_id else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        try:
            ch = await guild.create_text_channel(
                f"ticket-{interaction.user.name[:18].lower()}",
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites, reason="Ouverture de ticket")
            db.execute("INSERT OR REPLACE INTO open_tickets VALUES(?,?,?)", (ch.id,guild.id,interaction.user.id)); db.commit()
            embed = discord.Embed(title="🎫 Ticket ouvert", description=f"Bonjour {interaction.user.mention} ! Explique ta demande ici.", color=discord.Color.orange())
            if staff_role: embed.add_field(name="Équipe", value=staff_role.mention)
            await ch.send(content=interaction.user.mention, embed=embed, view=TicketCloseView())
            await interaction.response.send_message(f"Ton ticket est créé : {ch.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Je n'ai pas la permission de créer des salons. Vérifie Gérer les salons.", ephemeral=True)

class ConfigView(discord.ui.View):
    def __init__(self): super().__init__(timeout=240)
    @discord.ui.button(label="Filtre insultes ON/OFF", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def filter_toggle(self, interaction, button):
        ensure(interaction.guild_id)
        new = 1-int(val(interaction.guild_id,"insult_filter",1))
        db.execute("UPDATE settings SET insult_filter=? WHERE guild_id=?", (new,interaction.guild_id)); db.commit()
        await interaction.response.send_message(f"Filtre {'activé' if new else 'désactivé'}.", ephemeral=True)
    @discord.ui.button(label="État", style=discord.ButtonStyle.secondary, emoji="📊")
    async def status(self, interaction, button):
        g=interaction.guild
        await interaction.response.send_message(
            f"Filtre : {'ON' if val(g.id,'insult_filter',1) else 'OFF'}\n"
            f"Anti-raid : {'ON' if val(g.id,'raid_enabled',0) else 'OFF'}\n"
            f"Tickets : {'configurés' if val(g.id,'ticket_panel_channel') else 'à configurer'}\n"
            f"YouTube : {'configuré' if val(g.id,'youtube_channel_id') else 'à configurer'}", ephemeral=True)
    @discord.ui.button(label="Guide tickets", style=discord.ButtonStyle.secondary, emoji="🎫")
    async def ticket_guide(self, interaction, button):
        await interaction.response.send_message(
            "Configure `/tickets_config` puis lance `/tickets_panel` dans le salon choisi.", ephemeral=True)
    @discord.ui.button(label="Guide YouTube", style=discord.ButtonStyle.secondary, emoji="▶️")
    async def yt_guide(self, interaction, button):
        await interaction.response.send_message(
            "Configure `/youtube_config` avec l'ID de chaîne et le salon d'annonces. "
            "Pour le compteur d'abonnés, ajoute aussi YOUTUBE_API_KEY dans Render.", ephemeral=True)

@bot.event
async def on_ready():
    bot.add_view(TicketPanel())
    bot.add_view(TicketCloseView())
    try:
        synced = await bot.tree.sync()
        print(f"Connecté : {bot.user} | {len(synced)} commandes")
    except Exception as e: print("Sync failed:",repr(e))
    if not youtube_watch.is_running(): youtube_watch.start()
    if not counter_refresh.is_running(): counter_refresh.start()

@bot.tree.command(name="config", description="Ouvre la configuration interactive.")
@app_commands.checks.has_permissions(manage_guild=True)
async def config_cmd(interaction):
    ensure(interaction.guild_id)
    em=discord.Embed(title="ROYAL • Configuration", description=
      "🎫 Tickets : `/tickets_config`, `/tickets_panel`\n"
      "▶️ YouTube : `/youtube_config`, `/youtube_compteurs`\n"
      "📊 Compteurs : `/compteurs_config`\n"
      "🛡️ Modération : filtre, avertissements et sanctions\n"
      "🚨 Anti-raid : `/anti_raid`\n"
      "📋 Journaux : `/config_logs`", color=discord.Color.orange())
    await interaction.response.send_message(embed=em, view=ConfigView(), ephemeral=True)

@bot.tree.command(name="config_logs", description="Définit le salon des logs.")
@app_commands.checks.has_permissions(manage_guild=True)
async def config_logs(interaction, salon: discord.TextChannel):
    ensure(interaction.guild_id); db.execute("UPDATE settings SET log_channel=? WHERE guild_id=?",(salon.id,interaction.guild_id)); db.commit()
    await interaction.response.send_message(f"Logs : {salon.mention}",ephemeral=True)

@bot.tree.command(name="tickets_config", description="Configure catégorie, salon du panneau et rôle staff.")
@app_commands.checks.has_permissions(manage_guild=True)
async def tickets_config(interaction, categorie: discord.CategoryChannel, salon_panneau: discord.TextChannel, role_staff: discord.Role):
    ensure(interaction.guild_id)
    db.execute("UPDATE settings SET ticket_category=?,ticket_panel_channel=?,ticket_staff_role=? WHERE guild_id=?",
               (categorie.id,salon_panneau.id,role_staff.id,interaction.guild_id)); db.commit()
    await interaction.response.send_message(
        f"Tickets configurés.\nCatégorie : {categorie.mention}\nPanneau : {salon_panneau.mention}\nStaff : {role_staff.mention}\n"
        "Vérifie que le bot peut gérer les salons.",ephemeral=True)

@bot.tree.command(name="tickets_panel", description="Publie le panneau pour ouvrir des tickets.")
@app_commands.checks.has_permissions(manage_guild=True)
async def tickets_panel(interaction):
    cid=val(interaction.guild_id,"ticket_panel_channel")
    ch=interaction.guild.get_channel(cid) if cid else None
    if not isinstance(ch,discord.TextChannel):
        return await interaction.response.send_message("Configure d'abord `/tickets_config`.",ephemeral=True)
    em=discord.Embed(title="Besoin d'aide ?",description="Clique sur le bouton pour créer un ticket privé avec notre équipe.",color=discord.Color.orange())
    await ch.send(embed=em,view=TicketPanel())
    await interaction.response.send_message(f"Panneau publié dans {ch.mention}.",ephemeral=True)

@bot.tree.command(name="youtube_config", description="Configure le flux de vidéos YouTube.")
@app_commands.checks.has_permissions(manage_guild=True)
async def youtube_config(interaction, channel_id: app_commands.Range[str, 20, 30], salon_annonces: discord.TextChannel,
                         message: str = "🎬 Nouvelle vidéo sur la chaîne !\n{title}\n{url}"):
    ensure(interaction.guild_id)
    db.execute("UPDATE settings SET youtube_channel_id=?,youtube_feed_channel=?,youtube_template=? WHERE guild_id=?",
               (channel_id,salon_annonces.id,message[:1500],interaction.guild_id)); db.commit()
    await interaction.response.send_message(
        "Flux YouTube configuré. Le bot surveillera les nouvelles vidéos.\n"
        "Variables disponibles dans le message : `{title}`, `{url}`, `{channel}`.\n"
        "Pour le compteur d'abonnés, configure aussi `YOUTUBE_API_KEY` dans Render.",ephemeral=True)

@bot.tree.command(name="compteurs_config", description="Configure les salons compteurs.")
@app_commands.checks.has_permissions(manage_guild=True)
async def compteurs_config(interaction, salon_membres: discord.VoiceChannel, salon_abonnes: discord.VoiceChannel):
    ensure(interaction.guild_id)
    db.execute("UPDATE settings SET member_counter_channel=?,subscriber_counter_channel=? WHERE guild_id=?",
               (salon_membres.id,salon_abonnes.id,interaction.guild_id)); db.commit()
    await interaction.response.send_message("Salons compteurs configurés. Le bot mettra leurs noms à jour.",ephemeral=True)

@bot.tree.command(name="youtube_compteurs", description="Actualise le compteur YouTube si l'API est configurée.")
@app_commands.checks.has_permissions(manage_guild=True)
async def youtube_compteurs(interaction):
    if not YOUTUBE_API_KEY:
        return await interaction.response.send_message("Ajoute `YOUTUBE_API_KEY` dans Render > Environment.",ephemeral=True)
    await refresh_guild_counters(interaction.guild)
    await interaction.response.send_message("Actualisation demandée.",ephemeral=True)

async def refresh_guild_counters(guild):
    mc=val(guild.id,"member_counter_channel")
    sc=val(guild.id,"subscriber_counter_channel")
    if mc:
        channel=guild.get_channel(mc)
        if channel:
            try: await channel.edit(name=f"👥 Membres : {guild.member_count}")
            except discord.HTTPException: pass
    if sc and YOUTUBE_API_KEY:
        channel_id=val(guild.id,"youtube_channel_id")
        if channel_id:
            try:
                query=urllib.parse.urlencode({"part":"statistics","id":channel_id,"key":YOUTUBE_API_KEY})
                req=urllib.request.Request("https://www.googleapis.com/youtube/v3/channels?"+query)
                with urllib.request.urlopen(req,timeout=10) as response: data=json.loads(response.read())
                items=data.get("items",[])
                if items:
                    count=items[0].get("statistics",{}).get("subscriberCount")
                    hidden=items[0].get("statistics",{}).get("hiddenSubscriberCount",False)
                    ch=guild.get_channel(sc)
                    if ch and count is not None and not hidden:
                        await ch.edit(name=f"▶ Abonnés : {int(count):,}".replace(","," "))
            except Exception as e: print("YT stats failed:",repr(e))

@tasks.loop(minutes=20)
async def counter_refresh():
    for guild in bot.guilds:
        await refresh_guild_counters(guild)

@counter_refresh.before_loop
async def before_counter_refresh(): await bot.wait_until_ready()

@tasks.loop(minutes=2)
async def youtube_watch():
    # YouTube channel Atom feeds are public and do not require OAuth.
    ns={"yt":"http://www.youtube.com/xml/schemas/2015","atom":"http://www.w3.org/2005/Atom"}
    for guild in bot.guilds:
        cid=val(guild.id,"youtube_channel_id")
        out_id=val(guild.id,"youtube_feed_channel")
        out=guild.get_channel(out_id) if out_id else None
        if not cid or not isinstance(out,discord.TextChannel): continue
        try:
            url="https://www.youtube.com/feeds/videos.xml?channel_id="+urllib.parse.quote(cid)
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req,timeout=12) as res: xml=res.read()
            root=ET.fromstring(xml)
            entries=root.findall("atom:entry",ns)
            if not entries: continue
            newest=entries[0]
            vid=newest.findtext("yt:videoId",default="",namespaces=ns)
            title=newest.findtext("atom:title",default="Nouvelle vidéo",namespaces=ns)
            if not vid or val(guild.id,"youtube_last_video")==vid: continue
            # On first setup, remember the latest existing video without announcing old uploads.
            old=val(guild.id,"youtube_last_video")
            db.execute("UPDATE settings SET youtube_last_video=? WHERE guild_id=?",(vid,guild.id)); db.commit()
            if not old: continue
            link="https://www.youtube.com/watch?v="+vid
            template=val(guild.id,"youtube_template") or "🎬 Nouvelle vidéo !\n{title}\n{url}"
            content=template.replace("{title}",title).replace("{url}",link).replace("{channel}",guild.name)
            await out.send(content[:1900],allowed_mentions=discord.AllowedMentions.none())
        except Exception as e: print("YT feed failed:",repr(e))

@youtube_watch.before_loop
async def before_youtube_watch(): await bot.wait_until_ready()

@bot.tree.command(name="anti_raid", description="Active/désactive la quarantaine des nouveaux arrivants.")
@app_commands.checks.has_permissions(manage_guild=True)
async def anti_raid(interaction, activer: bool, seuil: app_commands.Range[int,3,50]=8, fenetre: app_commands.Range[int,5,120]=20):
    ensure(interaction.guild_id)
    db.execute("UPDATE settings SET raid_enabled=?,raid_threshold=?,raid_window=? WHERE guild_id=?",
               (int(activer),seuil,fenetre,interaction.guild_id)); db.commit()
    await interaction.response.send_message(f"Mode raid {'activé' if activer else 'désactivé'} : seuil {seuil}/{fenetre}s.",ephemeral=True)

@bot.tree.command(name="config_quarantaine", description="Configure le rôle de quarantaine et le salon.")
@app_commands.checks.has_permissions(manage_guild=True)
async def config_quarantaine(interaction, role: discord.Role, salon: discord.TextChannel):
    ensure(interaction.guild_id)
    db.execute("UPDATE settings SET quarantine_role=?,quarantine_channel=? WHERE guild_id=?",(role.id,salon.id,interaction.guild_id)); db.commit()
    await interaction.response.send_message("Configure les permissions du rôle : accès uniquement au salon de quarantaine.",ephemeral=True)

@bot.event
async def on_member_join(member):
    gid=member.guild.id; ensure(gid); now=time.time()
    arr=joins.setdefault(gid,[]); arr[:]=[t for t in arr if now-t<val(gid,"raid_window",20)]; arr.append(now)
    if val(gid,"raid_enabled",0):
        rid=val(gid,"quarantine_role"); role=member.guild.get_role(rid) if rid else None
        if role:
            try: await member.add_roles(role,reason="Mode anti-raid actif")
            except discord.Forbidden: await log(member.guild,"⚠️ Rôle de quarantaine impossible à appliquer : vérifie les permissions.")
        if len(arr)>=val(gid,"raid_threshold",8):
            await log(member.guild,f"🚨 Pic d'arrivées détecté : {len(arr)} en peu de temps. Les nouveaux arrivants sont en quarantaine.")
    await refresh_guild_counters(member.guild)

@bot.event
async def on_member_remove(member):
    await refresh_guild_counters(member.guild)

@bot.tree.command(name="liberer", description="Retire le rôle de quarantaine.")
@app_commands.checks.has_permissions(moderate_members=True)
async def liberer(interaction,membre:discord.Member):
    rid=val(interaction.guild_id,"quarantine_role"); role=interaction.guild.get_role(rid) if rid else None
    if not role or role not in membre.roles:
        return await interaction.response.send_message("Ce membre n'est pas en quarantaine.",ephemeral=True)
    await membre.remove_roles(role,reason=f"Libération par {interaction.user}")
    await interaction.response.send_message(f"{membre.mention} libéré.")
    await log(interaction.guild,f"🔓 {membre.mention} libéré par {interaction.user.mention}")

@bot.tree.command(name="avertir", description="Ajoute un avertissement manuel.")
@app_commands.checks.has_permissions(moderate_members=True)
async def avertir(interaction,membre:discord.Member,raison:str="Non précisée"):
    db.execute("INSERT INTO warnings VALUES(?,?,?,?,?)",(interaction.guild_id,membre.id,interaction.user.id,raison[:400],time.time())); db.commit()
    n=warn_count(interaction.guild_id,membre.id)
    try:
        if n>=3:
            await membre.ban(reason="3 avertissements ou plus")
            action="3 avertissements : bannissement permanent."
        else:
            await membre.timeout(timedelta(hours=24),reason=f"Avertissement {n}/3 : {raison}")
            action=f"Avertissement {n}/3 et timeout de 24 heures."
    except discord.Forbidden: action=f"Avertissement {n}/3 enregistré, sanction impossible : vérifie la hiérarchie."
    await interaction.response.send_message(f"{membre.mention} — {action}")
    await log(interaction.guild,f"⚠️ {membre.mention}: avertissement {n}/3 par {interaction.user.mention}. {raison}")

@bot.tree.command(name="avertissements",description="Consulte les avertissements d'un membre.")
@app_commands.checks.has_permissions(moderate_members=True)
async def avertissements(interaction,membre:discord.Member):
    rows=db.execute("SELECT moderator_id,reason,created FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created",(interaction.guild_id,membre.id)).fetchall()
    if not rows: return await interaction.response.send_message("Aucun avertissement.",ephemeral=True)
    text="\n".join(f"• <t:{int(t)}:d> — <@{mid}> — {reason}" for mid,reason,t in rows)
    await interaction.response.send_message(f"**{len(rows)} avertissement(s)**\n{text}",ephemeral=True)

@bot.tree.command(name="retirer_avertissement",description="Retire le dernier avertissement.")
@app_commands.checks.has_permissions(moderate_members=True)
async def retirer_avertissement(interaction,membre:discord.Member):
    row=db.execute("SELECT rowid FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created DESC LIMIT 1",(interaction.guild_id,membre.id)).fetchone()
    if not row: return await interaction.response.send_message("Aucun avertissement.",ephemeral=True)
    db.execute("DELETE FROM warnings WHERE rowid=?",(row[0],)); db.commit()
    await interaction.response.send_message(f"Avertissement retiré. Il en reste {warn_count(interaction.guild_id,membre.id)}.",ephemeral=True)

@bot.tree.command(name="terme_ajouter",description="Ajoute un mot/terme au filtre du serveur.")
@app_commands.checks.has_permissions(manage_guild=True)
async def terme_ajouter(interaction,terme:app_commands.Range[str,2,60]):
    term=norm(terme)
    if term:
        try: db.execute("INSERT INTO custom_terms VALUES(?,?)",(interaction.guild_id,term)); db.commit()
        except sqlite3.IntegrityError: pass
    await interaction.response.send_message("Terme ajouté (ou déjà présent).",ephemeral=True)

@bot.tree.command(name="terme_retirer",description="Retire un terme personnalisé.")
@app_commands.checks.has_permissions(manage_guild=True)
async def terme_retirer(interaction,terme:app_commands.Range[str,2,60]):
    db.execute("DELETE FROM custom_terms WHERE guild_id=? AND term=?",(interaction.guild_id,norm(terme))); db.commit()
    await interaction.response.send_message("Terme personnalisé retiré s'il existait.",ephemeral=True)

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    ensure(message.guild.id)
    if val(message.guild.id,"insult_filter",1):
        content=" "+norm(message.content)+" "
        hit=next((t for t in terms(message.guild.id) if " "+norm(t)+" " in content),None)
        member=message.author
        if hit and isinstance(member,discord.Member):
            if member.guild_permissions.manage_messages or member.guild_permissions.administrator: return
            try: await message.delete()
            except discord.Forbidden: pass
            db.execute("INSERT INTO warnings VALUES(?,?,?,?,?)",(message.guild.id,member.id,bot.user.id,"Insulte détectée automatiquement",time.time())); db.commit()
            n=warn_count(message.guild.id,member.id)
            try:
                if n>=3:
                    await member.ban(reason="3 avertissements automatiques pour messages insultants")
                    msg="Bannissement permanent après 3 avertissements."
                else:
                    await member.timeout(timedelta(hours=24),reason=f"Insulte détectée ({n}/3)")
                    msg=f"Avertissement {n}/3 : timeout 24h."
            except discord.Forbidden: msg=f"Avertissement {n}/3 enregistré, sanction impossible."
            try: await member.send(f"Le filtre de **{message.guild.name}** a détecté un message. {msg}")
            except discord.Forbidden: pass
            await log(message.guild,f"🛡️ Filtre : {member.mention}, avertissement {n}/3. {msg}")
            return
    await bot.process_commands(message)

@bot.tree.command(name="traduire",description="Traduit un texte à la demande.")
async def traduire(interaction,texte:app_commands.Range[str,1,450],source:str,cible:str):
    await interaction.response.defer(ephemeral=True)
    try:
        q=urllib.parse.urlencode({"q":texte,"langpair":f"{source}|{cible}"})
        with urllib.request.urlopen("https://api.mymemory.translated.net/get?"+q,timeout=12) as r: data=json.loads(r.read())
        out=data.get("responseData",{}).get("translatedText","")
        await interaction.followup.send(out[:1800] or "Traduction indisponible.",ephemeral=True)
    except Exception: await interaction.followup.send("Traduction indisponible. Réessaie plus tard.",ephemeral=True)

@bot.tree.error
async def command_error(interaction,error):
    msg="Permission insuffisante." if isinstance(error,app_commands.MissingPermissions) else "Erreur de commande. Vérifie les permissions et les paramètres."
    print("App command error:",repr(error))
    if interaction.response.is_done(): await interaction.followup.send(msg,ephemeral=True)
    else: await interaction.response.send_message(msg,ephemeral=True)

if not TOKEN: raise RuntimeError("Ajoute DISCORD_TOKEN dans Render > Environment.")
bot.run(TOKEN)
