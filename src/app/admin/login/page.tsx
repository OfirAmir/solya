export default function AdminLoginPage() {
  return (
    <main className="card">
      <h1>ניהול</h1>
      <form className="grid" action="/api/admin/login" method="post">
        <label>
          סיסמה
          <input name="password" type="password" required autoComplete="current-password" />
        </label>
        <button type="submit">כניסה</button>
      </form>
    </main>
  );
}
