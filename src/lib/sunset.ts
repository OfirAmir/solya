/** USNO-style sunset for a civil date. Accurate to a few minutes, enough to schedule +30. */
export function sunsetUtc(year: number, month: number, day: number, lat: number, lon: number): Date {
  const n1 = Math.floor((275 * month) / 9);
  const n2 = Math.floor((month + 9) / 12);
  const n3 = 1 + Math.floor((year - 4 * Math.floor(year / 4) + 2) / 3);
  const n = n1 - n2 * n3 + day - 30;
  const lngHour = lon / 15;
  const t = n + (18 - lngHour) / 24;
  const M = 0.9856 * t - 3.289;
  let L = M + 1.916 * Math.sin(deg(M)) + 0.02 * Math.sin(deg(2 * M)) + 282.634;
  L = wrap(L, 360);
  let RA = (Math.atan(0.91764 * Math.tan(deg(L))) * 180) / Math.PI;
  RA = wrap(RA, 360);
  const Lq = Math.floor(L / 90) * 90;
  const RAq = Math.floor(RA / 90) * 90;
  RA = (RA + (Lq - RAq)) / 15;
  const sinDec = 0.39782 * Math.sin(deg(L));
  const cosDec = Math.cos(Math.asin(sinDec));
  const cosH =
    (Math.cos(deg(90.833)) - sinDec * Math.sin(deg(lat))) / (cosDec * Math.cos(deg(lat)));
  const clamped = Math.min(1, Math.max(-1, cosH));
  const H = (Math.acos(clamped) * 180) / Math.PI;
  const T = H / 15 + RA - 0.06571 * t - 6.622;
  let UT = T - lngHour;
  UT = wrap(UT, 24);
  const hours = Math.floor(UT);
  const minutesFloat = (UT - hours) * 60;
  const minutes = Math.floor(minutesFloat);
  const seconds = Math.floor((minutesFloat - minutes) * 60);
  return new Date(Date.UTC(year, month - 1, day, hours, minutes, seconds));
}

export function eveningSendAt(
  localDate: string,
  lat: number,
  lon: number,
  delayMinutes = 30,
): Date {
  const [year, month, day] = localDate.split("-").map(Number);
  const sunset = sunsetUtc(year, month, day, lat, lon);
  return new Date(sunset.getTime() + delayMinutes * 60_000);
}

export function isInSendWindow(now: Date, sendAt: Date, windowHours: number): boolean {
  const start = sendAt.getTime();
  const end = start + windowHours * 60 * 60 * 1000;
  const t = now.getTime();
  return t >= start && t < end;
}

function deg(value: number): number {
  return (value * Math.PI) / 180;
}

function wrap(value: number, mod: number): number {
  return ((value % mod) + mod) % mod;
}
