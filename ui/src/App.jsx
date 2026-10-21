import { Route, Routes } from 'react-router-dom'

import { AppLayout } from './components/AppLayout'
import { DatabaseDetail } from './pages/DatabaseDetail'
import { Databases } from './pages/Databases'
import { InstanceDetail } from './pages/InstanceDetail'
import { Instances } from './pages/Instances'
import { NotFound } from './pages/NotFound'
import { Overview } from './pages/Overview'
import { ReplicationDetail } from './pages/ReplicationDetail'
import { Replications } from './pages/Replications'
import { UserDetail } from './pages/UserDetail'
import { Users } from './pages/Users'

export function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Overview />} />
        <Route path="/instances" element={<Instances />} />
        <Route path="/instances/:name" element={<InstanceDetail />} />
        <Route path="/databases" element={<Databases />} />
        <Route path="/databases/:namespace/:name" element={<DatabaseDetail />} />
        <Route path="/users" element={<Users />} />
        <Route path="/users/:namespace/:name" element={<UserDetail />} />
        <Route path="/replications" element={<Replications />} />
        <Route path="/replications/:namespace/:name" element={<ReplicationDetail />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
