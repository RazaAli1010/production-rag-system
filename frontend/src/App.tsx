import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Chat } from "./pages/Chat";

// Chat is the initial chunk; everything else is lazy so the first paint on 3G carries only what
// the student came for (NFR-1).
const Sources = lazy(() => import("./pages/Sources").then((m) => ({ default: m.Sources })));
const Admin = lazy(() => import("./pages/Admin").then((m) => ({ default: m.Admin })));
const Login = lazy(() => import("./pages/Login").then((m) => ({ default: m.Login })));
const Register = lazy(() => import("./pages/Login").then((m) => ({ default: m.Register })));

/**
 * AC-35: this guard is presentation only. `/internal/*` carries a router-level
 * `require_role("admin")` dependency server-side, so hiding the route is a courtesy, never the
 * control.
 */
function AdminOnly({ children }: { children: React.ReactNode }) {
  const { isAdmin, loading } = useAuth();
  if (loading) return null;
  return isAdmin ? <>{children}</> : <Navigate to="/" replace />;
}

export function App() {
  return (
    <AuthProvider>
      <Suspense fallback={null}>
        <Routes>
          <Route path="/" element={<Chat />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route
            path="/admin"
            element={
              <AdminOnly>
                <Admin />
              </AdminOnly>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </AuthProvider>
  );
}
