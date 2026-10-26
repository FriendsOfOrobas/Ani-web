-- Ani-Web: przewijanie czolowki i zakonczenia.
-- Czasy (sekundy, z AniSkip) przychodza przez script-opts, np.
--   script-opts=aniweb_skip-op_start=4,aniweb_skip-op_end=94
-- Skrypt jest wlasny (nie kopia ani-skip, ktory jest na GPLv3); logika jest prosta:
-- gdy pozycja wpadnie w przedzial, skacze na jego koniec — raz na przedzial, zeby
-- reczne cofniecie nie zapetlalo przewijania.
local options = require("mp.options")

local o = { op_start = 0, op_end = 0, ed_start = 0, ed_end = 0 }
options.read_options(o, "aniweb_skip")

local done = { op = false, ed = false }

local function maybe_skip(kind, t)
  local from, to = o[kind .. "_start"], o[kind .. "_end"]
  if done[kind] or to <= from then return end
  if t >= from and t < to then
    done[kind] = true
    mp.set_property_number("time-pos", to)
    mp.osd_message(kind == "op" and "Pominięto czołówkę" or "Pominięto zakończenie", 2)
  end
end

mp.observe_property("time-pos", "number", function(_, t)
  if not t then return end
  maybe_skip("op", t)
  maybe_skip("ed", t)
end)
