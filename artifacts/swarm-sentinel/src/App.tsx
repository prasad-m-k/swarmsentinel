import { useEffect, useRef, type ReactNode } from 'react';
import { ClerkProvider, SignIn, SignUp, useClerk, useUser } from '@clerk/react';
import { publishableKeyFromHost } from '@clerk/react/internal';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import Dashboard from '@/pages/dashboard';
import { Route, Switch, useLocation, Router as WouterRouter } from 'wouter';

const queryClient = new QueryClient();
const basePath = import.meta.env.BASE_URL.replace(/\/$/, '');
const clerkPubKey = publishableKeyFromHost(window.location.hostname, import.meta.env.VITE_CLERK_PUBLISHABLE_KEY);
const clerkProxyUrl = import.meta.env.VITE_CLERK_PROXY_URL;

function OwnedDashboard() {
  const { user, isLoaded } = useUser();
  if (!isLoaded) return <p className="p-6">Loading authentication…</p>;
  // Remount also clears local traces, selections and SSE on account changes.
  return <Dashboard key={user?.id ?? 'anonymous'} />;
}

function RoutedErrorBoundary({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  return <ErrorBoundary resetKey={location}>{children}</ErrorBoundary>;
}

function Router() {
  const [, setLocation] = useLocation();
  return (
    <ClerkProvider publishableKey={clerkPubKey} proxyUrl={clerkProxyUrl}
      signInUrl={`${basePath}/sign-in`} signUpUrl={`${basePath}/sign-up`}
      signInFallbackRedirectUrl={basePath || '/'} signUpFallbackRedirectUrl={basePath || '/'}
      routerPush={(to) => setLocation(basePath && to.startsWith(basePath) ? to.slice(basePath.length) || '/' : to)}
      routerReplace={(to) => setLocation(basePath && to.startsWith(basePath) ? to.slice(basePath.length) || '/' : to, { replace: true })}
      appearance={{ variables: { colorPrimary: '#3ddc97', colorBackground: '#111925', colorForeground: '#e8eef5', colorInput: '#1d2939', colorInputForeground: '#e8eef5', colorMutedForeground: '#a5b4c8', fontFamily: 'var(--app-font-sans)' } }}>
      <AuthCacheReset />
    <RoutedErrorBoundary>
      <Switch>
        <Route path="/" component={OwnedDashboard} />
        <Route path="/sign-in/*?"><div className="min-h-screen flex items-center justify-center"><SignIn routing="path" path={`${basePath}/sign-in`} signUpUrl={`${basePath}/sign-up`} /></div></Route>
        <Route path="/sign-up/*?"><div className="min-h-screen flex items-center justify-center"><SignUp routing="path" path={`${basePath}/sign-up`} signInUrl={`${basePath}/sign-in`} /></div></Route>
        <Route component={NotFound} />
      </Switch>
    </RoutedErrorBoundary>
    </ClerkProvider>
  );
}

function AuthCacheReset() {
  const { addListener } = useClerk();
  const previous = useRef<string | null | undefined>(undefined);
  useEffect(() => addListener(({ user }) => {
    const id = user?.id ?? null;
    if (previous.current !== undefined && previous.current !== id) queryClient.clear();
    previous.current = id;
  }), [addListener]);
  return null;
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
          <Router />
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
