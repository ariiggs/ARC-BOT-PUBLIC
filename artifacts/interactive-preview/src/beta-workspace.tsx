import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowLeft,
  Check,
  ChevronRight,
  CircleHelp,
  Clock3,
  Gamepad2,
  Hash,
  KeyRound,
  LockKeyhole,
  Map,
  RefreshCw,
  Server,
  ShieldCheck,
  Trophy,
  UserRound,
  UsersRound,
} from "lucide-react";

type AuthUser = {
  id: string;
  username: string;
  displayName: string;
  avatar: string | null;
};

type BetaSlot = {
  number: number;
  status: string;
  team_name: string;
  tag: string;
  manager_id: number | null;
  assignment_id: number;
  captain_1_id: number | null;
  captain_2_id: number | null;
};

type BetaScore = {
  match_number: number;
  slot_number: number;
  kills: number;
  placement: number;
};

type BetaIdPwConfig = {
  scrim_id: string;
  target_channel_id: number;
  fixed_password: string;
  timezone_name: string;
  announcement_message_id: number | null;
};

type BetaScrim = {
  id: string;
  guild_id: number;
  name: string;
  public_channel_id: number;
  staff_channel_id: number;
  staff_role_id: number | null;
  pending_role_id: number | null;
  confirmed_role_id: number | null;
  cap_channel_id: number | null;
  logs_channel_id: number | null;
  history_channel_id: number | null;
  registration_channel_id: number | null;
  registration_role_id: number | null;
  registration_auto_accept: boolean;
  emoji_available: string;
  emoji_reserved: string;
  emoji_pending: string;
  emoji_confirmed: string;
  public_message_id: number | null;
  staff_message_id: number | null;
  is_open: boolean;
  registration_open: boolean;
  slot_start: number;
  slot_end: number;
  slots: BetaSlot[];
  timezone: string;
  maps: string[];
  max_matches: number;
  match_maps: string[];
  pw_type: string;
  fixed_pw: string;
  current_match_counter: number;
  kill_points_value: number;
  placement_points_string: string;
  leaderboard_layout: string;
  leaderboard_background: string;
  match_scores: BetaScore[];
  pending_registrations: Array<{
    request_id: string;
    slot_number: number;
    team_name: string;
    tag: string;
    manager_id: number;
    assignment_id: number;
    registration_message_id: number | null;
  }>;
  idpw_config: BetaIdPwConfig | null;
};

type BetaServerConfig = {
  guild_id: number;
  head_staff_role_id: number | null;
  staff_role_id: number | null;
  logs_channel_id: number | null;
  license_type: string;
} | null;

type BetaSnapshot = {
  guild_id: number;
  server_config: BetaServerConfig;
  scrims: BetaScrim[];
};

type LiveTab = "Overview" | "Scrims" | "Results" | "Server";

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-[18px] border border-[#27333c] bg-[#121b23] ${className}`}>
      {children}
    </section>
  );
}

function Label({ children }: { children: ReactNode }) {
  return (
    <p className="mono mb-1.5 text-[9px] font-bold uppercase tracking-[0.18em] text-[#aa8b4b]">
      {children}
    </p>
  );
}

function Pill({ children, tone = "green" }: { children: ReactNode; tone?: "green" | "gold" | "muted" }) {
  const styles = {
    green: "border-[#91c8a5]/25 bg-[#91c8a5]/[0.08] text-[#9dc9aa]",
    gold: "border-[#d6b66f]/25 bg-[#d6b66f]/[0.08] text-[#d6b66f]",
    muted: "border-[#52616a]/30 bg-[#52616a]/[0.08] text-[#a6b0b3]",
  };
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[9px] font-semibold ${styles[tone]}`}>
      {children}
    </span>
  );
}

function formatId(value: number | null | undefined) {
  return value ? String(value) : "Not configured";
}

function scrimStatus(scrim: BetaScrim) {
  if (!scrim.is_open) return { label: "Closed", tone: "muted" as const };
  if (!scrim.registration_open) return { label: "Registration closed", tone: "gold" as const };
  return { label: "Open", tone: "green" as const };
}

function occupiedSlots(scrim: BetaScrim) {
  return scrim.slots.filter((slot) => slot.status !== "Disponible");
}

function currentMap(scrim: BetaScrim) {
  return scrim.match_maps[scrim.current_match_counter - 1] ?? scrim.maps[0] ?? "Not configured";
}

function slotStatusTone(status: string) {
  if (status === "Confirmé") return "text-[#9dc9aa]";
  if (status === "En attente") return "text-[#e4c47b]";
  if (status === "En attente du manager") return "text-[#9fc3d3]";
  return "text-[#7f8d94]";
}

function OverviewTab({ snapshot, onSelectScrim }: { snapshot: BetaSnapshot; onSelectScrim: (id: string) => void }) {
  const totalSlots = snapshot.scrims.reduce((sum, scrim) => sum + scrim.slots.length, 0);
  const occupied = snapshot.scrims.reduce((sum, scrim) => sum + occupiedSlots(scrim).length, 0);
  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <div className="relative border-b border-[#27333c] bg-[linear-gradient(135deg,#35444e_0%,#24333c_52%,#17232c_100%)] p-5 md:p-7">
          <div className="absolute -right-14 -top-20 h-48 w-48 rounded-full border border-[#d6b66f]/20" />
          <div className="relative">
            <Label>ARC BETA / LIVE SNAPSHOT</Label>
            <h1 className="display text-[26px] font-semibold tracking-[-0.05em] text-[#edf0ef]">Discord operations</h1>
            <p className="mt-2 max-w-xl text-[11px] leading-5 text-[#b4c0c3]">
              Données lues depuis le stockage SQLite existant du bot beta. Aucun résultat automatique n’est activé.
            </p>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-px bg-[#27333c] sm:grid-cols-4">
          {[
            ["Scrims", String(snapshot.scrims.length)],
            ["Slots", `${occupied} / ${totalSlots}`],
            ["Open", String(snapshot.scrims.filter((scrim) => scrim.is_open).length)],
            ["Guild", String(snapshot.guild_id)],
          ].map(([name, value]) => (
            <div className="min-w-0 bg-[#121b23] p-3.5" key={name}>
              <Label>{name}</Label>
              <p className="truncate text-[13px] font-semibold text-[#e4eae8]">{value}</p>
            </div>
          ))}
        </div>
      </Card>

      <Card className="p-4 md:p-5">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <Label>Live scrims</Label>
            <h2 className="display text-[21px] font-semibold text-[#e5eae9]">Current schedule</h2>
          </div>
          <Pill><Activity className="h-3 w-3" /> Bot sync</Pill>
        </div>
        <div className="space-y-2">
          {snapshot.scrims.map((scrim) => {
            const status = scrimStatus(scrim);
            return (
              <button
                type="button"
                key={scrim.id}
                onClick={() => onSelectScrim(scrim.id)}
                className="group flex w-full items-center gap-3 rounded-xl border border-[#293640] bg-[#18232c]/75 p-3.5 text-left hover:border-[#b89249]/50"
              >
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-[#d6b66f]/25 bg-[#d6b66f]/[0.08] text-[#d6b66f]">
                  <Gamepad2 className="h-4 w-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[11px] font-semibold text-[#d5dcdc]">{scrim.name}</span>
                  <span className="mt-1 block truncate text-[9px] text-[#74818a]">
                    {scrim.max_matches} matches · {scrim.timezone} · map {currentMap(scrim)}
                  </span>
                </span>
                <span className="hidden text-right sm:block">
                  <span className="block text-[10px] font-semibold text-[#bcc6c5]">{occupiedSlots(scrim).length} / {scrim.slots.length} slots</span>
                  <span className="mt-1 block text-[9px] text-[#74818a]">{status.label}</span>
                </span>
                <ChevronRight className="h-4 w-4 text-[#53616a]" />
              </button>
            );
          })}
        </div>
      </Card>

      <div className="flex items-start gap-3 rounded-[16px] border border-[#304450] bg-[#14232b] p-4">
        <CircleHelp className="mt-0.5 h-4 w-4 shrink-0 text-[#9fc3d3]" />
        <p className="text-[10px] leading-5 text-[#8faeb8]">
          <span className="font-semibold text-[#b9d8e4]">Bot-process bridge.</span> Les lectures et écritures passent par le même processus Python que ARC Beta. Les résultats automatiques restent désactivés.
        </p>
      </div>
    </div>
  );
}

function ServerTab({ snapshot }: { snapshot: BetaSnapshot }) {
  const config = snapshot.server_config;
  return (
    <div className="space-y-4">
      <Card className="p-4 md:p-5">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div><Label>Beta server</Label><h1 className="display text-[23px] font-semibold text-[#e5eae9]">Server parameters</h1><p className="mt-1 text-[11px] text-[#849097]">Configuration persisted by the ARC bot.</p></div>
          <Pill tone="gold"><Server className="h-3 w-3" /> Beta</Pill>
        </div>
        <div className="space-y-2">
          <ValueRow label="Guild ID" value={String(snapshot.guild_id)} icon={<Server className="h-3.5 w-3.5" />} />
          <ValueRow label="License" value={config?.license_type ?? "Not configured"} icon={<ShieldCheck className="h-3.5 w-3.5" />} />
          <ValueRow label="Head staff role" value={formatId(config?.head_staff_role_id)} icon={<LockKeyhole className="h-3.5 w-3.5" />} />
          <ValueRow label="Staff role" value={formatId(config?.staff_role_id)} icon={<UsersRound className="h-3.5 w-3.5" />} />
          <ValueRow label="Logs channel" value={formatId(config?.logs_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
        </div>
      </Card>
      <Card className="p-4">
        <Label>Data boundary</Label>
        <p className="text-[10px] leading-5 text-[#89979e]">This page reads only guild {snapshot.guild_id}. The API does not resolve missing guild context to another scrim, and it does not read or modify A.R.C. Public.</p>
      </Card>
    </div>
  );
}

function ValueRow({ label, value, icon }: { label: string; value: string; icon: ReactNode }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-[#293640] bg-[#18232c]/70 p-3">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[#d6b66f]/[0.08] text-[#c8a962]">{icon}</span>
      <span className="min-w-0 flex-1 text-[10px] font-semibold text-[#bdc8c8]">{label}</span>
      <span className="max-w-[58%] truncate text-right font-['DM_Mono'] text-[9px] text-[#d6b66f]">{value}</span>
    </div>
  );
}

type ScrimMutation = (
  path: string,
  init: RequestInit,
  successMessage: string,
) => Promise<void>;

type EditorState = {
  name: string;
  public_channel_id: string;
  staff_channel_id: string;
  staff_role_id: string;
  pending_role_id: string;
  confirmed_role_id: string;
  cap_channel_id: string;
  logs_channel_id: string;
  history_channel_id: string;
  registration_channel_id: string;
  registration_role_id: string;
  registration_auto_accept: boolean;
  slot_start: string;
  slot_end: string;
  max_matches: string;
  maps: string;
  match_maps: string;
  kill_points_value: string;
  placement_points_string: string;
  leaderboard_layout: string;
  leaderboard_background: string;
  pw_type: string;
  fixed_password: string;
  timezone_name: string;
  target_channel_id: string;
  emoji_available: string;
  emoji_reserved: string;
  emoji_pending: string;
  emoji_confirmed: string;
};

function editorState(scrim: BetaScrim): EditorState {
  const idpw = scrim.idpw_config;
  return {
    name: scrim.name,
    public_channel_id: String(scrim.public_channel_id),
    staff_channel_id: String(scrim.staff_channel_id),
    staff_role_id: scrim.staff_role_id ? String(scrim.staff_role_id) : "",
    pending_role_id: scrim.pending_role_id ? String(scrim.pending_role_id) : "",
    confirmed_role_id: scrim.confirmed_role_id ? String(scrim.confirmed_role_id) : "",
    cap_channel_id: scrim.cap_channel_id ? String(scrim.cap_channel_id) : "",
    logs_channel_id: scrim.logs_channel_id ? String(scrim.logs_channel_id) : "",
    history_channel_id: scrim.history_channel_id ? String(scrim.history_channel_id) : "",
    registration_channel_id: scrim.registration_channel_id ? String(scrim.registration_channel_id) : "",
    registration_role_id: scrim.registration_role_id ? String(scrim.registration_role_id) : "",
    registration_auto_accept: scrim.registration_auto_accept,
    slot_start: String(scrim.slot_start),
    slot_end: String(scrim.slot_end),
    max_matches: String(scrim.max_matches),
    maps: scrim.maps.join(", "),
    match_maps: scrim.match_maps.join(", "),
    kill_points_value: String(scrim.kill_points_value),
    placement_points_string: scrim.placement_points_string,
    leaderboard_layout: scrim.leaderboard_layout,
    leaderboard_background: scrim.leaderboard_background,
    pw_type: scrim.pw_type,
    fixed_password: scrim.fixed_pw || idpw?.fixed_password || "",
    timezone_name: scrim.timezone,
    target_channel_id: idpw?.target_channel_id ? String(idpw.target_channel_id) : "",
    emoji_available: scrim.emoji_available,
    emoji_reserved: scrim.emoji_reserved,
    emoji_pending: scrim.emoji_pending,
    emoji_confirmed: scrim.emoji_confirmed,
  };
}

function optionalNumber(value: string) {
  const trimmed = value.trim();
  return trimmed ? Number(trimmed) : null;
}

function EditorField({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[9px] font-semibold uppercase tracking-[0.08em] text-[#829099]">{label}</span>
      <input type={type} value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className="w-full rounded-lg border border-[#354650] bg-[#101820] px-2.5 py-2 text-[10px] text-[#d9e1df] outline-none focus:border-[#d6b66f]/60" />
    </label>
  );
}

function ScrimEditor({
  scrim,
  onCancel,
  onSave,
}: {
  scrim: BetaScrim;
  onCancel: () => void;
  onSave: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [form, setForm] = useState(() => editorState(scrim));
  useEffect(() => setForm(editorState(scrim)), [scrim]);
  const update = <K extends keyof EditorState>(key: K, value: EditorState[K]) =>
    setForm((current) => ({ ...current, [key]: value }));
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await onSave({
      name: form.name.trim(),
      public_channel_id: Number(form.public_channel_id),
      staff_channel_id: Number(form.staff_channel_id),
      staff_role_id: optionalNumber(form.staff_role_id),
      pending_role_id: optionalNumber(form.pending_role_id),
      confirmed_role_id: optionalNumber(form.confirmed_role_id),
      cap_channel_id: optionalNumber(form.cap_channel_id),
      logs_channel_id: optionalNumber(form.logs_channel_id),
      history_channel_id: optionalNumber(form.history_channel_id),
      registration_channel_id: optionalNumber(form.registration_channel_id),
      registration_role_id: optionalNumber(form.registration_role_id),
      registration_auto_accept: form.registration_auto_accept,
      slot_start: Number(form.slot_start),
      slot_end: Number(form.slot_end),
      max_matches: Number(form.max_matches),
      maps: form.maps.split(",").map((item) => item.trim()).filter(Boolean),
      match_maps: form.match_maps.split(",").map((item) => item.trim()).filter(Boolean),
      kill_points_value: Number(form.kill_points_value),
      placement_points_string: form.placement_points_string,
      leaderboard_layout: form.leaderboard_layout,
      leaderboard_background: form.leaderboard_background,
      idpw: {
        target_channel_id: Number(form.target_channel_id),
        fixed_password: form.pw_type === "fixed" ? form.fixed_password : "",
        password_type: form.pw_type,
        timezone_name: form.timezone_name,
      },
      emojis: {
        emoji_available: form.emoji_available,
        emoji_reserved: form.emoji_reserved,
        emoji_pending: form.emoji_pending,
        emoji_confirmed: form.emoji_confirmed,
      },
    });
  };
  return (
    <form onSubmit={submit} className="space-y-5">
      <div className="flex items-start justify-between gap-3"><div><Label>Edit configuration</Label><h2 className="display text-[22px] font-semibold text-[#e5eae9]">{scrim.name}</h2><p className="mt-1 text-[10px] text-[#849097]">The bot validates IDs, collisions, maps and password settings before saving.</p></div><button type="button" onClick={onCancel} className="rounded-lg border border-[#354650] px-3 py-2 text-[10px] text-[#aeb8bd]">Cancel</button></div>
      <section><Label>Identity and Discord references</Label><div className="grid gap-3 sm:grid-cols-2">
        <EditorField label="Name" value={form.name} onChange={(value) => update("name", value)} />
        <EditorField label="Public channel ID" value={form.public_channel_id} onChange={(value) => update("public_channel_id", value)} />
        <EditorField label="Staff channel ID" value={form.staff_channel_id} onChange={(value) => update("staff_channel_id", value)} />
        <EditorField label="ID/PW target channel ID" value={form.target_channel_id} onChange={(value) => update("target_channel_id", value)} />
        <EditorField label="Logs channel ID" value={form.logs_channel_id} onChange={(value) => update("logs_channel_id", value)} />
        <EditorField label="History channel ID" value={form.history_channel_id} onChange={(value) => update("history_channel_id", value)} />
        <EditorField label="!cap channel ID" value={form.cap_channel_id} onChange={(value) => update("cap_channel_id", value)} />
        <EditorField label="Registration channel ID" value={form.registration_channel_id} onChange={(value) => update("registration_channel_id", value)} />
      </div></section>
      <section><Label>Roles and registration</Label><div className="grid gap-3 sm:grid-cols-2">
        <EditorField label="Staff role ID" value={form.staff_role_id} onChange={(value) => update("staff_role_id", value)} />
        <EditorField label="Pending captain role ID" value={form.pending_role_id} onChange={(value) => update("pending_role_id", value)} />
        <EditorField label="Confirmed captain role ID" value={form.confirmed_role_id} onChange={(value) => update("confirmed_role_id", value)} />
        <EditorField label="Registration role ID" value={form.registration_role_id} onChange={(value) => update("registration_role_id", value)} />
      </div><label className="mt-3 flex items-center gap-2 text-[10px] text-[#c4cdcd]"><input type="checkbox" checked={form.registration_auto_accept} onChange={(event) => update("registration_auto_accept", event.target.checked)} /> Auto-accept team registrations</label></section>
      <section><Label>Matches and maps</Label><div className="grid gap-3 sm:grid-cols-3">
        <EditorField label="First slot" type="number" value={form.slot_start} onChange={(value) => update("slot_start", value)} />
        <EditorField label="Last slot" type="number" value={form.slot_end} onChange={(value) => update("slot_end", value)} />
        <EditorField label="Match count" type="number" value={form.max_matches} onChange={(value) => update("max_matches", value)} />
        <EditorField label="Map pool (comma separated)" value={form.maps} onChange={(value) => update("maps", value)} />
        <EditorField label="Match rotation (comma separated)" value={form.match_maps} onChange={(value) => update("match_maps", value)} />
        <EditorField label="Timezone" value={form.timezone_name} onChange={(value) => update("timezone_name", value)} />
      </div></section>
      <section><Label>ID/PW and leaderboard</Label><div className="grid gap-3 sm:grid-cols-2">
        <label className="block"><span className="mb-1 block text-[9px] font-semibold uppercase tracking-[0.08em] text-[#829099]">Password mode</span><select value={form.pw_type} onChange={(event) => update("pw_type", event.target.value)} className="w-full rounded-lg border border-[#354650] bg-[#101820] px-2.5 py-2 text-[10px] text-[#d9e1df]"><option value="dynamic">Dynamic</option><option value="fixed">Fixed</option></select></label>
        <EditorField label="Fixed password" value={form.fixed_password} onChange={(value) => update("fixed_password", value)} />
        <EditorField label="Kill points" type="number" value={form.kill_points_value} onChange={(value) => update("kill_points_value", value)} />
        <EditorField label="Placement points" value={form.placement_points_string} onChange={(value) => update("placement_points_string", value)} />
        <EditorField label="Leaderboard layout" value={form.leaderboard_layout} onChange={(value) => update("leaderboard_layout", value)} />
        <EditorField label="Leaderboard background" value={form.leaderboard_background} onChange={(value) => update("leaderboard_background", value)} />
      </div></section>
      <section><Label>Slot legend emojis</Label><div className="grid gap-3 sm:grid-cols-2">
        <EditorField label="Available" value={form.emoji_available} onChange={(value) => update("emoji_available", value)} />
        <EditorField label="Reserved" value={form.emoji_reserved} onChange={(value) => update("emoji_reserved", value)} />
        <EditorField label="Pending" value={form.emoji_pending} onChange={(value) => update("emoji_pending", value)} />
        <EditorField label="Confirmed" value={form.emoji_confirmed} onChange={(value) => update("emoji_confirmed", value)} />
      </div></section>
      <button type="submit" className="w-full rounded-xl border border-[#d6b66f]/40 bg-[#d6b66f] px-4 py-3 text-[11px] font-bold text-[#18201e]">Save through ARC Beta bot</button>
    </form>
  );
}

function NewScrimForm({
  onCancel,
  onSave,
}: {
  onCancel: () => void;
  onSave: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [form, setForm] = useState<EditorState>({
    name: "",
    public_channel_id: "",
    staff_channel_id: "",
    staff_role_id: "",
    pending_role_id: "",
    confirmed_role_id: "",
    cap_channel_id: "",
    logs_channel_id: "",
    history_channel_id: "",
    registration_channel_id: "",
    registration_role_id: "",
    registration_auto_accept: false,
    slot_start: "3",
    slot_end: "25",
    max_matches: "4",
    maps: "",
    match_maps: "",
    kill_points_value: "1",
    placement_points_string: "10 6 5 4 3 2 1",
    leaderboard_layout: "1_col",
    leaderboard_background: "reference",
    pw_type: "dynamic",
    fixed_password: "",
    timezone_name: "UTC",
    target_channel_id: "",
    emoji_available: "🟢",
    emoji_reserved: "🟡",
    emoji_pending: "🔵",
    emoji_confirmed: "✅",
  });
  const update = <K extends keyof EditorState>(key: K, value: EditorState[K]) =>
    setForm((current) => ({ ...current, [key]: value }));
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await onSave({
      name: form.name.trim(),
      public_channel_id: Number(form.public_channel_id),
      staff_channel_id: Number(form.staff_channel_id),
      staff_role_id: optionalNumber(form.staff_role_id),
      pending_role_id: optionalNumber(form.pending_role_id),
      confirmed_role_id: optionalNumber(form.confirmed_role_id),
      cap_channel_id: optionalNumber(form.cap_channel_id),
      logs_channel_id: Number(form.logs_channel_id),
      history_channel_id: Number(form.history_channel_id),
      registration_channel_id: optionalNumber(form.registration_channel_id),
      registration_role_id: optionalNumber(form.registration_role_id),
      registration_auto_accept: form.registration_auto_accept,
      is_open: false,
      slot_start: Number(form.slot_start),
      slot_end: Number(form.slot_end),
      max_matches: Number(form.max_matches),
      maps: form.maps.split(",").map((item) => item.trim()).filter(Boolean),
      match_maps: form.match_maps.split(",").map((item) => item.trim()).filter(Boolean),
      kill_points_value: Number(form.kill_points_value),
      placement_points_string: form.placement_points_string,
      leaderboard_layout: form.leaderboard_layout,
      leaderboard_background: form.leaderboard_background,
      idpw: {
        target_channel_id: Number(form.target_channel_id),
        fixed_password: form.pw_type === "fixed" ? form.fixed_password : "",
        password_type: form.pw_type,
        timezone_name: form.timezone_name,
      },
    });
  };
  return (
    <form onSubmit={submit} className="space-y-5">
      <div className="flex items-start justify-between gap-3"><div><Label>New beta scrim</Label><h1 className="display text-[23px] font-semibold text-[#e5eae9]">Setup wizard</h1><p className="mt-1 text-[10px] leading-5 text-[#849097]">The bot will validate every Discord reference before creating the scrim.</p></div><button type="button" onClick={onCancel} className="rounded-lg border border-[#354650] px-3 py-2 text-[10px] text-[#aeb8bd]">Cancel</button></div>
      <div className="grid gap-3 sm:grid-cols-2">
        <EditorField label="Name" value={form.name} onChange={(value) => update("name", value)} />
        <EditorField label="Public channel ID" value={form.public_channel_id} onChange={(value) => update("public_channel_id", value)} />
        <EditorField label="Staff channel ID" value={form.staff_channel_id} onChange={(value) => update("staff_channel_id", value)} />
        <EditorField label="ID/PW target channel ID" value={form.target_channel_id} onChange={(value) => update("target_channel_id", value)} />
        <EditorField label="Logs channel ID" value={form.logs_channel_id} onChange={(value) => update("logs_channel_id", value)} />
        <EditorField label="History channel ID" value={form.history_channel_id} onChange={(value) => update("history_channel_id", value)} />
        <EditorField label="Pending captain role ID" value={form.pending_role_id} onChange={(value) => update("pending_role_id", value)} />
        <EditorField label="Confirmed captain role ID" value={form.confirmed_role_id} onChange={(value) => update("confirmed_role_id", value)} />
        <EditorField label="!cap channel ID" value={form.cap_channel_id} onChange={(value) => update("cap_channel_id", value)} />
        <EditorField label="Staff role ID (optional)" value={form.staff_role_id} onChange={(value) => update("staff_role_id", value)} />
        <EditorField label="First slot" type="number" value={form.slot_start} onChange={(value) => update("slot_start", value)} />
        <EditorField label="Last slot" type="number" value={form.slot_end} onChange={(value) => update("slot_end", value)} />
        <EditorField label="Match count" type="number" value={form.max_matches} onChange={(value) => update("max_matches", value)} />
        <EditorField label="Map pool (comma separated)" value={form.maps} onChange={(value) => update("maps", value)} />
        <EditorField label="Match rotation (comma separated)" value={form.match_maps} onChange={(value) => update("match_maps", value)} />
        <EditorField label="Timezone" value={form.timezone_name} onChange={(value) => update("timezone_name", value)} />
        <EditorField label="Fixed password" value={form.fixed_password} onChange={(value) => update("fixed_password", value)} />
      </div>
      <label className="flex items-center gap-2 text-[10px] text-[#c4cdcd]"><input type="checkbox" checked={form.registration_auto_accept} onChange={(event) => update("registration_auto_accept", event.target.checked)} /> Auto-accept team registrations</label>
      <button type="submit" className="w-full rounded-xl border border-[#d6b66f]/40 bg-[#d6b66f] px-4 py-3 text-[11px] font-bold text-[#18201e]">Create through ARC Beta bot</button>
    </form>
  );
}

function ScrimDetail({ scrim, onBack, onMutate }: { scrim: BetaScrim; onBack: () => void; onMutate: ScrimMutation }) {
  const [selectedSlot, setSelectedSlot] = useState<BetaSlot | null>(null);
  const [editing, setEditing] = useState(false);
  const status = scrimStatus(scrim);
  const idpw = scrim.idpw_config;
  if (editing) {
    return (
      <div className="space-y-4">
        <button type="button" onClick={() => setEditing(false)} className="inline-flex items-center gap-2 text-[10px] font-semibold text-[#b9c3c4]"><ArrowLeft className="h-3.5 w-3.5" /> Back to scrim</button>
        <Card className="p-4 md:p-5"><ScrimEditor scrim={scrim} onCancel={() => setEditing(false)} onSave={async (body) => { await onMutate(`/api/scrims/${scrim.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, "Scrim configuration saved."); setEditing(false); }} /></Card>
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <button type="button" onClick={onBack} className="inline-flex items-center gap-2 text-[10px] font-semibold text-[#b9c3c4]"><ArrowLeft className="h-3.5 w-3.5" /> All beta scrims</button>
      <Card className="overflow-hidden">
        <div className="border-b border-[#27333c] bg-[#1d2825] p-4 md:p-5">
          <div className="flex items-start justify-between gap-3">
            <div><Label>Live scrim</Label><h1 className="display text-[24px] font-semibold tracking-[-0.05em] text-[#e5eae9]">{scrim.name}</h1><p className="mt-1 font-['DM_Mono'] text-[9px] text-[#8c999f]">{scrim.id} · {scrim.timezone}</p></div>
            <div className="flex flex-wrap justify-end gap-2"><Pill tone={status.tone}>{status.label}</Pill><button type="button" onClick={() => setEditing(true)} className="rounded-lg border border-[#d6b66f]/35 bg-[#d6b66f]/[0.1] px-2.5 py-1.5 text-[9px] font-semibold text-[#e5c984]">Edit</button></div>
          </div>
          <div className="flex flex-wrap gap-2 border-b border-[#27333c] px-4 pb-4 md:px-5">
            <button type="button" onClick={() => onMutate(`/api/scrims/${scrim.id}/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: scrim.is_open ? "close" : "open" }) }, scrim.is_open ? "Scrim closed." : "Scrim opened.")} className="rounded-lg border border-[#354650] px-3 py-2 text-[9px] text-[#b8c3c4]">{scrim.is_open ? "Close scrim" : "Open scrim"}</button>
            <button type="button" onClick={() => onMutate(`/api/scrims/${scrim.id}/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "reset_counter" }) }, "Match counter reset.")} className="rounded-lg border border-[#354650] px-3 py-2 text-[9px] text-[#b8c3c4]">Reset match counter</button>
            <button type="button" onClick={() => { if (window.confirm(`Delete ${scrim.name}?`)) void onMutate(`/api/scrims/${scrim.id}`, { method: "DELETE" }, "Scrim deleted."); }} className="rounded-lg border border-[#654044] bg-[#2a1b20] px-3 py-2 text-[9px] text-[#e2a6a9]">Delete scrim</button>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Pill tone="muted"><UsersRound className="h-3 w-3" /> {occupiedSlots(scrim).length} / {scrim.slots.length} slots</Pill>
            <Pill tone="muted"><Trophy className="h-3 w-3" /> {scrim.max_matches} matches</Pill>
            <Pill tone="muted"><Map className="h-3 w-3" /> Match {scrim.current_match_counter}: {currentMap(scrim)}</Pill>
          </div>
        </div>
        <div className="space-y-5 p-4 md:p-5">
          <section>
            <Label>Scrim configuration</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              <ValueRow label="Public channel" value={formatId(scrim.public_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
              <ValueRow label="Staff channel" value={formatId(scrim.staff_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
              <ValueRow label="Slot range" value={`${scrim.slot_start} – ${scrim.slot_end}`} icon={<UsersRound className="h-3.5 w-3.5" />} />
              <ValueRow label="Registration" value={scrim.registration_open ? "Open" : "Closed"} icon={<Check className="h-3.5 w-3.5" />} />
              <ValueRow label="Maps" value={scrim.maps.join(" · ") || "None"} icon={<Map className="h-3.5 w-3.5" />} />
              <ValueRow label="Assigned rotation" value={scrim.match_maps.join(" · ") || "None"} icon={<Map className="h-3.5 w-3.5" />} />
              <ValueRow label="Kill points" value={String(scrim.kill_points_value)} icon={<Trophy className="h-3.5 w-3.5" />} />
              <ValueRow label="Placement points" value={scrim.placement_points_string} icon={<Trophy className="h-3.5 w-3.5" />} />
              <ValueRow label="Staff role" value={formatId(scrim.staff_role_id)} icon={<LockKeyhole className="h-3.5 w-3.5" />} />
              <ValueRow label="Pending role" value={formatId(scrim.pending_role_id)} icon={<LockKeyhole className="h-3.5 w-3.5" />} />
              <ValueRow label="Confirmed role" value={formatId(scrim.confirmed_role_id)} icon={<LockKeyhole className="h-3.5 w-3.5" />} />
              <ValueRow label="Logs channel" value={formatId(scrim.logs_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
              <ValueRow label="Cap channel" value={formatId(scrim.cap_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
              <ValueRow label="History channel" value={formatId(scrim.history_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
              <ValueRow label="Registration channel" value={formatId(scrim.registration_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
              <ValueRow label="Registration role" value={formatId(scrim.registration_role_id)} icon={<LockKeyhole className="h-3.5 w-3.5" />} />
              <ValueRow label="Registration auto-accept" value={scrim.registration_auto_accept ? "Enabled" : "Disabled"} icon={<Check className="h-3.5 w-3.5" />} />
            </div>
          </section>

          <section>
            <Label>Board references</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              <ValueRow label="Public board message" value={formatId(scrim.public_message_id)} icon={<Activity className="h-3.5 w-3.5" />} />
              <ValueRow label="Staff board message" value={formatId(scrim.staff_message_id)} icon={<Activity className="h-3.5 w-3.5" />} />
              <ValueRow label="Leaderboard layout" value={scrim.leaderboard_layout} icon={<Trophy className="h-3.5 w-3.5" />} />
              <ValueRow label="Leaderboard background" value={scrim.leaderboard_background} icon={<Trophy className="h-3.5 w-3.5" />} />
              <ValueRow label="Available emoji" value={scrim.emoji_available} icon={<Check className="h-3.5 w-3.5" />} />
              <ValueRow label="Reserved emoji" value={scrim.emoji_reserved} icon={<Check className="h-3.5 w-3.5" />} />
              <ValueRow label="Pending emoji" value={scrim.emoji_pending} icon={<Check className="h-3.5 w-3.5" />} />
              <ValueRow label="Confirmed emoji" value={scrim.emoji_confirmed} icon={<Check className="h-3.5 w-3.5" />} />
            </div>
          </section>

          <section>
            <Label>Room credentials</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              <ValueRow label="Password type" value={scrim.pw_type} icon={<KeyRound className="h-3.5 w-3.5" />} />
              <ValueRow label="Fixed password" value={scrim.fixed_pw || idpw?.fixed_password || "Not configured"} icon={<KeyRound className="h-3.5 w-3.5" />} />
              <ValueRow label="Target channel" value={formatId(idpw?.target_channel_id)} icon={<Hash className="h-3.5 w-3.5" />} />
              <ValueRow label="Announcement" value={formatId(idpw?.announcement_message_id)} icon={<Activity className="h-3.5 w-3.5" />} />
            </div>
          </section>

          <section>
            <div className="mb-2 flex items-center justify-between"><Label>Slots</Label><span className="text-[9px] text-[#74818a]">Select a slot to inspect</span></div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {scrim.slots.map((slot) => (
                <button type="button" key={slot.number} onClick={() => setSelectedSlot(slot)} className={`rounded-xl border p-3 text-left transition-colors ${selectedSlot?.number === slot.number ? "border-[#d6b66f]/50 bg-[#d6b66f]/[0.1]" : "border-[#293640] bg-[#18232c]/70 hover:border-[#52616a]"}`}>
                  <span className="flex items-center justify-between"><span className="font-['DM_Mono'] text-[11px] font-bold text-[#d6b66f]">#{String(slot.number).padStart(2, "0")}</span><span className={`text-[9px] font-semibold ${slotStatusTone(slot.status)}`}>{slot.status}</span></span>
                  <span className="mt-2 block truncate text-[10px] text-[#c4cdcd]">{slot.team_name || "Available"}</span>
                  <span className="mt-1 block truncate text-[9px] text-[#74818a]">{slot.tag || "No tag"}</span>
                </button>
              ))}
            </div>
            {selectedSlot && (
              <div className="mt-3 rounded-xl border border-[#d6b66f]/25 bg-[#201e18] p-3 text-[10px] text-[#c9bd9e]">
                <div className="flex items-center justify-between"><span className="font-semibold text-[#e3c57f]">Slot #{selectedSlot.number}</span><span>{selectedSlot.status}</span></div>
                <p className="mt-2">Team: {selectedSlot.team_name || "Available"} · Tag: {selectedSlot.tag || "—"}</p>
                <p className="mt-1 text-[#998f78]">Captain 1: {formatId(selectedSlot.captain_1_id)} · Captain 2: {formatId(selectedSlot.captain_2_id)}</p>
              </div>
            )}
          </section>

          <section>
            <Label>Pending registrations</Label>
            {scrim.pending_registrations.length === 0 ? (
              <p className="rounded-xl border border-dashed border-[#354650] p-3 text-[10px] text-[#778890]">No pending registration requests are stored.</p>
            ) : (
              <div className="space-y-2">
                {scrim.pending_registrations.map((request) => (
                  <div key={request.request_id} className="rounded-xl border border-[#293640] bg-[#18232c]/70 p-3 text-[10px]">
                    <div className="flex items-center justify-between"><span className="font-semibold text-[#d5dcdc]">{request.team_name} · {request.tag}</span><span className="text-[#d6b66f]">Slot #{request.slot_number}</span></div>
                    <p className="mt-1 text-[#74818a]">Manager {request.manager_id} · Assignment {request.assignment_id}</p>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </Card>
    </div>
  );
}

function ScrimsTab({ snapshot, selectedId, onSelect, onBack, onMutate }: { snapshot: BetaSnapshot; selectedId: string | null; onSelect: (id: string) => void; onBack: () => void; onMutate: ScrimMutation }) {
  const [creating, setCreating] = useState(false);
  const selected = snapshot.scrims.find((scrim) => scrim.id === selectedId);
  if (selected) return <ScrimDetail scrim={selected} onBack={onBack} onMutate={onMutate} />;
  if (creating) {
    return <Card className="p-4 md:p-5"><NewScrimForm onCancel={() => setCreating(false)} onSave={async (body) => { await onMutate("/api/scrims", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, "Scrim created."); setCreating(false); }} /></Card>;
  }
  return (
    <div className="space-y-4">
      <Card className="p-4 md:p-5">
        <div className="mb-5 flex items-start justify-between gap-3"><div><Label>Workspace schedule</Label><h1 className="display text-[23px] font-semibold text-[#e5eae9]">Manage beta scrims</h1><p className="mt-1 text-[11px] text-[#849097]">The same setup fields as `!setup`, backed by the beta bot.</p></div><button type="button" onClick={() => setCreating(true)} className="rounded-lg border border-[#d6b66f]/35 bg-[#d6b66f]/[0.1] px-3 py-2 text-[10px] font-semibold text-[#e5c984]">New scrim</button></div>
        <div className="space-y-2">{snapshot.scrims.map((scrim) => { const status = scrimStatus(scrim); return <button type="button" data-testid={`button-open-live-scrim-${scrim.id}`} key={scrim.id} onClick={() => onSelect(scrim.id)} className="group flex w-full items-center gap-3 rounded-xl border border-[#293640] bg-[#18232c]/75 p-3.5 text-left hover:border-[#b89249]/50"><span className="flex h-10 w-10 items-center justify-center rounded-xl border border-[#d6b66f]/25 bg-[#d6b66f]/[0.08] text-[#d6b66f]"><Gamepad2 className="h-4 w-4" /></span><span className="min-w-0 flex-1"><span className="block truncate text-[11px] font-semibold text-[#d5dcdc]">{scrim.name}</span><span className="mt-1 block truncate text-[9px] text-[#74818a]">{scrim.max_matches} matches · {scrim.maps.join(" / ")}</span></span><span className="text-right"><span className="block text-[10px] font-semibold text-[#bcc6c5]">{occupiedSlots(scrim).length} / {scrim.slots.length}</span><span className={`mt-1 block text-[9px] ${status.tone === "green" ? "text-[#9dc9aa]" : "text-[#d6b66f]"}`}>{status.label}</span></span><ChevronRight className="h-4 w-4 text-[#53616a]" /></button>; })}</div>
      </Card>
    </div>
  );
}

function ResultsTab({ snapshot }: { snapshot: BetaSnapshot }) {
  return (
    <div className="space-y-4">
      <Card className="p-4 md:p-5">
        <div className="mb-5 flex items-start justify-between gap-3"><div><Label>Result workflow</Label><h1 className="display text-[23px] font-semibold text-[#e5eae9]">Scrim results</h1><p className="mt-1 text-[11px] text-[#849097]">Existing manual scores are visible from the beta snapshot.</p></div><Pill tone="muted"><LockKeyhole className="h-3 w-3" /> Auto disabled</Pill></div>
        <div className="space-y-3">{snapshot.scrims.map((scrim) => <div key={scrim.id} className="rounded-xl border border-[#293640] bg-[#18232c]/70 p-3.5"><div className="flex items-center justify-between"><div><p className="text-[11px] font-semibold text-[#d5dcdc]">{scrim.name}</p><p className="mt-1 text-[9px] text-[#74818a]">Match {scrim.current_match_counter} · {scrim.match_maps[scrim.current_match_counter - 1] ?? "No map assigned"}</p></div><Pill tone="muted"><UserRound className="h-3 w-3" /> Manual only</Pill></div>{scrim.match_scores.length === 0 ? <p className="mt-4 rounded-lg border border-dashed border-[#354650] p-3 text-[10px] leading-5 text-[#778890]">No manual scores are stored for this scrim yet.</p> : <div className="mt-3 space-y-1">{scrim.match_scores.map((score) => <div key={`${score.match_number}-${score.slot_number}`} className="flex items-center justify-between rounded-lg bg-[#121b23] px-3 py-2 text-[10px]"><span>Match {score.match_number} · Slot #{score.slot_number}</span><span className="text-[#d6b66f]">{score.kills} kills · placement {score.placement}</span></div>)}</div>}</div>)}</div>
      </Card>
      <div className="rounded-[16px] border border-[#304450] bg-[#14232b] p-4 text-[10px] leading-5 text-[#8faeb8]"><span className="font-semibold text-[#b9d8e4]">Automatic results are not selectable.</span> Screenshot reading and automatic result selection remain in development, as requested.</div>
    </div>
  );
}

function LoadingState() {
  return <div className="flex min-h-[360px] items-center justify-center rounded-[18px] border border-[#27333c] bg-[#121b23]"><div className="text-center"><RefreshCw className="mx-auto h-5 w-5 animate-spin text-[#d6b66f]" /><p className="mono mt-3 text-[9px] uppercase tracking-[0.16em] text-[#71808a]">Reading beta snapshot</p></div></div>;
}

export function BetaWorkspace({ user, onLogout }: { user: AuthUser; onLogout: () => void }) {
  const [activeTab, setActiveTab] = useState<LiveTab>("Overview");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const query = useQuery<BetaSnapshot>({
    queryKey: ["beta-snapshot"],
    queryFn: async () => {
      const response = await fetch("/api/scrims", { credentials: "include" });
      if (!response.ok) throw new Error("Beta snapshot unavailable");
      return (await response.json()) as BetaSnapshot;
    },
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });

  const mutate: ScrimMutation = async (path, init, successMessage) => {
    const response = await fetch(path, { ...init, credentials: "include" });
    const payload = (await response.json().catch(() => ({}))) as { error?: string };
    if (!response.ok) {
      const message = payload.error ?? "The beta bot bridge is unavailable.";
      setNotice(message);
      throw new Error(message);
    }
    await query.refetch();
    setNotice(successMessage);
  };

  const navigate = (tab: LiveTab) => {
    setActiveTab(tab);
    if (tab !== "Scrims") setSelectedId(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const tabs: Array<[LiveTab, ReactNode]> = [
    ["Overview", <Activity className="h-4 w-4" />],
    ["Scrims", <Gamepad2 className="h-4 w-4" />],
    ["Results", <Trophy className="h-4 w-4" />],
    ["Server", <Server className="h-4 w-4" />],
  ];

  return (
    <div className="grid-noise min-h-[100dvh] bg-[#0b1117] text-[#dfe6e5]">
      <div className="relative mx-auto flex min-h-[100dvh] w-full max-w-[1220px] overflow-hidden border-x border-[#1c2932] bg-[#0f171e]">
        <aside className="hidden w-[220px] shrink-0 border-r border-[#202d36] bg-[#0e161d] p-4 lg:block">
          <div className="mb-8 flex items-center gap-3 px-2"><span className="flex h-10 w-10 items-center justify-center rounded-xl border border-[#d6b66f]/35 bg-[#d6b66f]/[0.12] font-bold text-[#e3c57f]">A.</span><div><p className="display text-[13px] font-semibold text-[#dbe2e1]">ARC Beta</p><p className="mono text-[8px] uppercase tracking-[0.13em] text-[#6f7d85]">Live workspace</p></div></div>
          <p className="mono mb-2 px-2 text-[9px] font-bold uppercase tracking-[0.18em] text-[#6b7b84]">Workspace</p>
          <div className="space-y-1">{tabs.map(([label, icon]) => <button type="button" key={label} onClick={() => navigate(label)} className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[10px] font-semibold ${activeTab === label ? "bg-[#d6b66f]/[0.12] text-[#e5c984]" : "text-[#788890] hover:bg-[#17232b] hover:text-[#dce4e2]"}`}>{icon}{label}</button>)}</div>
          <div className="mt-10 rounded-2xl border border-[#2e382e] bg-[#171e1b] p-3"><p className="mono text-[8px] uppercase tracking-[0.16em] text-[#a4874d]">Access verified</p><p className="mt-2 text-[10px] leading-5 text-[#929b92]">Discord role checked for Bot manager. Data is scoped to the beta guild.</p></div>
        </aside>
        <div className="min-w-0 flex-1">
          <header className="sticky top-0 z-30 flex h-[66px] items-center justify-between border-b border-[#25313a]/90 bg-[#0f171e]/95 px-4 backdrop-blur-md lg:px-8">
            <div><p className="mono text-[9px] uppercase tracking-[0.18em] text-[#6f7d85]">ARC BETA / {activeTab}</p><p className="display mt-1 text-[15px] font-semibold text-[#dfe7e5]">Live Discord operations</p></div>
            <div className="flex items-center gap-2"><Pill><Activity className="h-3 w-3" /> Live</Pill><span className="flex h-7 w-7 items-center justify-center overflow-hidden rounded-full bg-[#d6b66f]/15 font-['DM_Mono'] text-[9px] font-bold text-[#dfc177]">{user.avatar ? <img src={user.avatar} alt="" className="h-full w-full object-cover" /> : user.displayName.slice(0, 2).toUpperCase()}</span><button type="button" onClick={onLogout} className="rounded-lg px-2 py-1.5 text-[9px] text-[#7d8b93] hover:bg-[#1c2932] hover:text-[#e4c77f]">Sign out</button></div>
          </header>
          <main className="relative mx-auto max-w-[900px] px-4 pb-28 pt-5 lg:px-8 lg:pb-12 lg:pt-8">
            {query.isLoading && <LoadingState />}
            {query.isError && <div className="rounded-[18px] border border-[#654044] bg-[#2a1b20] p-5 text-[11px] leading-5 text-[#e2a6a9]">The beta snapshot could not be read. Check that the existing SQLite file is available to the API workspace, then reload.</div>}
            {notice && <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-[#d6b66f]/25 bg-[#201e18] px-3 py-2.5 text-[10px] text-[#d8c798]"><span>{notice}</span><button type="button" onClick={() => setNotice("")} className="text-[#a99d7e]">Dismiss</button></div>}
            {query.data && <div className="rise-in">{activeTab === "Overview" && <OverviewTab snapshot={query.data} onSelectScrim={(id) => { setSelectedId(id); setActiveTab("Scrims"); }} />}{activeTab === "Scrims" && <ScrimsTab snapshot={query.data} selectedId={selectedId} onSelect={setSelectedId} onBack={() => setSelectedId(null)} onMutate={mutate} />}{activeTab === "Results" && <ResultsTab snapshot={query.data} />}{activeTab === "Server" && <ServerTab snapshot={query.data} />}</div>}
          </main>
        </div>
        <nav className="fixed bottom-0 left-1/2 z-50 flex w-full max-w-[430px] -translate-x-1/2 border-t border-[#293640] bg-[#111a22]/95 px-2 pb-[max(9px,env(safe-area-inset-bottom))] pt-2 backdrop-blur-lg lg:hidden">{tabs.map(([label, icon]) => <button type="button" key={label} onClick={() => navigate(label)} className={`flex min-w-0 flex-1 flex-col items-center gap-1 rounded-xl py-1.5 text-[9px] font-semibold ${activeTab === label ? "text-[#e5c984]" : "text-[#728089]"}`}><span className={`relative flex h-6 w-8 items-center justify-center rounded-lg ${activeTab === label ? "bg-[#d6b66f]/[0.13]" : ""}`}>{icon}</span>{label}</button>)}</nav>
      </div>
    </div>
  );
}