// 种子样本(TypeScript):覆盖第二种语言,验证多语言解析。
// 保持小:接口、类、跨方法调用。

export interface Repo {
  find(id: string): string | null;
}

export class MemoryRepo implements Repo {
  private store: Map<string, string> = new Map();

  find(id: string): string | null {
    return this.store.get(id) ?? null;
  }

  save(id: string, value: string): void {
    this.store.set(id, value);
  }
}

export class Service {
  constructor(private repo: Repo) {}

  // 调用 repo.find,制造调用边
  lookup(id: string): string {
    const found = this.repo.find(id);
    return found ?? "not-found";
  }
}

// 入口函数:实例化并调用,形成调用链
export function bootstrap(): string {
  const repo = new MemoryRepo();
  repo.save("k1", "v1");
  const svc = new Service(repo);
  return svc.lookup("k1");
}
