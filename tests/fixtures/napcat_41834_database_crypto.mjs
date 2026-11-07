// Reference capsule copied from NapCat 4.18.34, never its executable entry.
// https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.34
// Upstream napcat.mjs SHA256:
// 6a22c93373c8f73462658d57b00bc88164d7e5858c0fe2f178eb97d66d7cb3ba
const T8 = 1024, Qs = 4096, CN = 16, TN = 32, dY = 16, fY = 48, hY = 4e3, mY = 2, gY = 58, yY = Buffer.from("SQLite header 3\0"), vY = Buffer.from("SQLite format 3\0");
function bY(t, e) {
  const n = aI(t, e, hY, TN, "sha512"), r = Buffer.alloc(e.length);
  for (let o = 0; o < e.length; o++)
    r[o] = e[o] ^ gY;
  const i = aI(n, r, mY, TN, "sha512");
  return { encKey: n, hmacKey: i };
}
function xY(t, e, n = 0) {
  const r = t.subarray(n), i = r.length - fY, o = r.subarray(0, i), s = r.subarray(i, i + dY), a = GL("aes-256-cbc", e, s);
  return a.setAutoPadding(!1), Buffer.concat([a.update(o), a.final()]);
}
function k8(t) {
  return t.length < T8 + Qs ? !1 : t.subarray(0, 16).equals(yY);
}
function WE(t, e) {
  if (!k8(t)) return null;
  const n = t.subarray(T8), r = Math.floor(n.length / Qs);
  if (r === 0) return null;
  const i = n.subarray(0, CN), { encKey: o } = bY(e, i), s = Buffer.alloc(r * Qs);
  let a = 0;
  for (let c = 1; c <= r; c++) {
    const u = (c - 1) * Qs, l = n.subarray(u, u + Qs), f = xY(l, o, c === 1 ? CN : 0);
    c === 1 ? (vY.copy(s, 0), f.copy(s, 16), s.writeUInt16BE(Qs, 16), a = Qs) : (f.copy(s, a), a += Qs);
  }
  return s;
}
