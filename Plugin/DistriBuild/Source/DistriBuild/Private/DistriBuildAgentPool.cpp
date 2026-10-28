#include "DistriBuildAgentPool.h"

#include "DistriBuildProtocol.h"
#include "HAL/Event.h"
#include "HAL/PlatformProcess.h"
#include "HAL/RunnableThread.h"
#include "IPAddress.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "SocketSubsystem.h"
#include "Sockets.h"

using FAgentPtr = TSharedPtr<FDistriBuildAgent, ESPMode::ThreadSafe>;

FDistriBuildAgentPool::FDistriBuildAgentPool()
{
	WakeEvent = FPlatformProcess::GetSynchEventFromPool(false);
}

FDistriBuildAgentPool::~FDistriBuildAgentPool()
{
	Shutdown();
	FPlatformProcess::ReturnSynchEventToPool(WakeEvent);
	WakeEvent = nullptr;
}

void FDistriBuildAgentPool::Start(const FDistriBuildConfig& InConfig)
{
	ApplyConfig(InConfig);
	if (!Thread)
	{
		bStopping = false;
		Thread = FRunnableThread::Create(this, TEXT("DistriBuildAgentPool"), 128 * 1024, TPri_BelowNormal);
	}
}

void FDistriBuildAgentPool::Shutdown()
{
	if (Thread)
	{
		bStopping = true;
		WakeEvent->Trigger();
		Thread->WaitForCompletion();
		delete Thread;
		Thread = nullptr;
	}
}

void FDistriBuildAgentPool::ApplyConfig(const FDistriBuildConfig& InConfig)
{
	FScopeLock Guard(&Lock);
	Config = InConfig;
	// Static agents that are no longer listed are dropped; discovered ones age out on their own.
	Agents.RemoveAll([this](const FAgentPtr& Agent)
	{
		if (!Agent->bStatic || Agent->InFlight > 0)
		{
			return false;
		}
		for (const FString& Entry : Config.Agents)
		{
			FString Host;
			int32 Port;
			if (DistriBuildProtocol::ParseAddress(Entry, Host, Port) && Host == Agent->Host && Port == Agent->Port)
			{
				return false;
			}
		}
		return true;
	});
}

void FDistriBuildAgentPool::RequestRefresh()
{
	WakeEvent->Trigger();
}

uint32 FDistriBuildAgentPool::Run()
{
	while (!bStopping)
	{
		RefreshNow();
		float Interval;
		{
			FScopeLock Guard(&Lock);
			Interval = Config.RefreshIntervalSeconds;
		}
		WakeEvent->Wait(FTimespan::FromSeconds(Interval));
	}
	return 0;
}

void FDistriBuildAgentPool::RefreshNow()
{
	FDistriBuildConfig Cfg;
	{
		FScopeLock Guard(&Lock);
		Cfg = Config;
	}
	if (!Cfg.bEnabled)
	{
		return;
	}

	for (const FString& Entry : Cfg.Agents)
	{
		FString Host;
		int32 Port;
		if (!DistriBuildProtocol::ParseAddress(Entry, Host, Port))
		{
			continue;
		}
		TSharedPtr<FJsonObject> Info;
		if (QueryAgent(Host, Port, Info, Cfg))
		{
			UpdateFromInfo(Host, Info, true);
		}
		else
		{
			FScopeLock Guard(&Lock);
			const FAgentPtr* Existing = Agents.FindByPredicate([&](const FAgentPtr& A) { return A->Host == Host && A->Port == Port; });
			FAgentPtr Agent = Existing ? *Existing : MakeShared<FDistriBuildAgent, ESPMode::ThreadSafe>();
			if (!Existing)
			{
				Agent->Host = Host;
				Agent->Port = Port;
				Agent->Name = Host;
				Agents.Add(Agent);
			}
			Agent->bStatic = true;
			Agent->bOnline = false;
		}
	}

	if (Cfg.bAutoDiscovery)
	{
		TArray<TPair<FString, TSharedPtr<FJsonObject>>> Replies;
		Discover(Cfg, Replies);
		for (const TPair<FString, TSharedPtr<FJsonObject>>& Reply : Replies)
		{
			UpdateFromInfo(Reply.Key, Reply.Value, false);
		}
	}

	// Discovered agents that stopped answering go offline, then disappear.
	const double Now = FPlatformTime::Seconds();
	FScopeLock Guard(&Lock);
	for (const FAgentPtr& Agent : Agents)
	{
		if (!Agent->bStatic && Now - Agent->LastSeen > Cfg.RefreshIntervalSeconds * 3)
		{
			Agent->bOnline = false;
		}
	}
	Agents.RemoveAll([&](const FAgentPtr& A)
	{
		return !A->bStatic && A->InFlight == 0 && Now - A->LastSeen > Cfg.RefreshIntervalSeconds * 30;
	});
}

bool FDistriBuildAgentPool::QueryAgent(const FString& Host, int32 Port, TSharedPtr<FJsonObject>& OutInfo, const FDistriBuildConfig& Cfg)
{
	FSocket* Socket = DistriBuildProtocol::Connect(Host, Port, Cfg.ConnectTimeoutSeconds);
	if (!Socket)
	{
		return false;
	}
	TSharedRef<FJsonObject> Request = MakeShared<FJsonObject>();
	Request->SetStringField(TEXT("type"), TEXT("info"));
	Request->SetStringField(TEXT("token"), Cfg.AuthToken);
	TArray<DistriBuildProtocol::FBlob> Blobs;
	const bool bOk = DistriBuildProtocol::SendFrame(Socket, Request, {})
		&& DistriBuildProtocol::RecvFrame(Socket, Cfg.ConnectTimeoutSeconds + 2.0f, OutInfo, Blobs)
		&& OutInfo->GetStringField(TEXT("type")) == TEXT("info");
	DistriBuildProtocol::Close(Socket);
	return bOk;
}

void FDistriBuildAgentPool::Discover(const FDistriBuildConfig& Cfg, TArray<TPair<FString, TSharedPtr<FJsonObject>>>& OutReplies)
{
	ISocketSubsystem* Subsystem = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
	if (!Subsystem)
	{
		return;
	}
	FSocket* Socket = Subsystem->CreateSocket(NAME_DGram, TEXT("DistriBuildDiscovery"), FNetworkProtocolTypes::IPv4);
	if (!Socket)
	{
		return;
	}
	Socket->SetBroadcast(true);
	Socket->SetNonBlocking(true);

	static const char Probe[] = "DBLD?";
	int32 Sent = 0;
	TSharedRef<FInternetAddr> Broadcast = Subsystem->CreateInternetAddr(FNetworkProtocolTypes::IPv4);
	Broadcast->SetBroadcastAddress();
	Broadcast->SetPort(Cfg.DiscoveryPort);
	Socket->SendTo(reinterpret_cast<const uint8*>(Probe), 5, Sent, *Broadcast);

	// Also probe this machine directly: broadcast does not loop back on every OS.
	TSharedRef<FInternetAddr> Loopback = Subsystem->CreateInternetAddr(FNetworkProtocolTypes::IPv4);
	Loopback->SetLoopbackAddress();
	Loopback->SetPort(Cfg.DiscoveryPort);
	Socket->SendTo(reinterpret_cast<const uint8*>(Probe), 5, Sent, *Loopback);

	TArray<uint8> Buffer;
	Buffer.SetNumUninitialized(64 * 1024);
	TSharedRef<FInternetAddr> From = Subsystem->CreateInternetAddr(FNetworkProtocolTypes::IPv4);
	const double Deadline = FPlatformTime::Seconds() + 0.7;
	while (FPlatformTime::Seconds() < Deadline && !bStopping)
	{
		if (!Socket->Wait(ESocketWaitConditions::WaitForRead, FTimespan::FromMilliseconds(100)))
		{
			continue;
		}
		int32 Read = 0;
		if (!Socket->RecvFrom(Buffer.GetData(), Buffer.Num(), Read, *From) || Read <= 0)
		{
			continue;
		}
		FUTF8ToTCHAR Converted(reinterpret_cast<const ANSICHAR*>(Buffer.GetData()), Read);
		TSharedPtr<FJsonObject> Info;
		TSharedRef<TJsonReader<TCHAR>> Reader = TJsonReaderFactory<TCHAR>::Create(FString(Converted.Length(), Converted.Get()));
		if (FJsonSerializer::Deserialize(Reader, Info) && Info.IsValid())
		{
			OutReplies.Emplace(From->ToString(false), Info);
		}
	}

	Socket->Close();
	Subsystem->DestroySocket(Socket);
}

void FDistriBuildAgentPool::UpdateFromInfo(const FString& Host, const TSharedPtr<FJsonObject>& Info, bool bStatic)
{
	const FString Name = Info->GetStringField(TEXT("name"));
	const int32 Port = int32(Info->GetNumberField(TEXT("port")));

	FScopeLock Guard(&Lock);
	// Match on address first, then on name+port: one machine may answer discovery from another
	// interface than the address in the static list, and must not be counted twice.
	FAgentPtr* Found = Agents.FindByPredicate([&](const FAgentPtr& A) { return A->Host == Host && A->Port == Port; });
	if (!Found)
	{
		Found = Agents.FindByPredicate([&](const FAgentPtr& A) { return A->Name == Name && A->Port == Port; });
	}
	FAgentPtr Agent = Found ? *Found : MakeShared<FDistriBuildAgent, ESPMode::ThreadSafe>();
	if (!Found)
	{
		Agent->Host = Host;
		Agent->Port = Port;
		Agents.Add(Agent);
		UE_LOG(LogDistriBuild, Log, TEXT("Agent %s (%s:%d) joined with %d slots"), *Name, *Host, Port, int32(Info->GetNumberField(TEXT("slots"))));
	}
	Agent->bStatic |= bStatic;
	Agent->Name = Name;
	Agent->Platform = Info->GetStringField(TEXT("platform"));
	Agent->Slots = int32(Info->GetNumberField(TEXT("slots")));
	Agent->Engines.Reset();
	const TArray<TSharedPtr<FJsonValue>>* Engines = nullptr;
	if (Info->TryGetArrayField(TEXT("engines"), Engines))
	{
		for (const TSharedPtr<FJsonValue>& Engine : *Engines)
		{
			Agent->Engines.Add(Engine->AsString());
		}
	}
	Agent->bOnline = true;
	Agent->LastSeen = FPlatformTime::Seconds();
}

FAgentPtr FDistriBuildAgentPool::Acquire(const FString& EngineKey)
{
	const double Now = FPlatformTime::Seconds();
	FScopeLock Guard(&Lock);
	FAgentPtr Best;
	float BestLoad = 1.0f;
	for (const FAgentPtr& Agent : Agents)
	{
		if (!Agent->bOnline || Agent->Slots <= 0 || Agent->InFlight >= Agent->Slots || Agent->SkipUntil > Now
			|| !Agent->Engines.Contains(EngineKey))
		{
			continue;
		}
		const float Load = float(Agent->InFlight) / float(Agent->Slots);
		if (Load < BestLoad)
		{
			BestLoad = Load;
			Best = Agent;
		}
	}
	if (Best)
	{
		++Best->InFlight;
	}
	return Best;
}

void FDistriBuildAgentPool::Release(const FAgentPtr& Agent, EDistriBuildOutcome Outcome)
{
	FScopeLock Guard(&Lock);
	--Agent->InFlight;
	const double Now = FPlatformTime::Seconds();
	switch (Outcome)
	{
	case EDistriBuildOutcome::Success:
		++Agent->Completed;
		Agent->ConsecutiveFailures = 0;
		break;
	case EDistriBuildOutcome::Busy:
		// Another client is using it; leave it alone for a moment.
		Agent->SkipUntil = Now + 1.0;
		break;
	case EDistriBuildOutcome::NetworkError:
		++Agent->Failed;
		Agent->SkipUntil = Now + 15.0;
		Agent->bOnline = false;
		WakeEvent->Trigger();
		break;
	case EDistriBuildOutcome::JobFailed:
		++Agent->Failed;
		if (++Agent->ConsecutiveFailures >= 5)
		{
			UE_LOG(LogDistriBuild, Warning, TEXT("Agent %s failed %d jobs in a row, pausing it for 60s"), *Agent->Name, Agent->ConsecutiveFailures);
			Agent->SkipUntil = Now + 60.0;
			Agent->ConsecutiveFailures = 0;
		}
		break;
	}
}

void FDistriBuildAgentPool::Snapshot(const FString& EngineKey, TArray<FDistriBuildAgentSnapshot>& Out) const
{
	const double Now = FPlatformTime::Seconds();
	FScopeLock Guard(&Lock);
	Out.Reset(Agents.Num());
	for (const FAgentPtr& Agent : Agents)
	{
		FDistriBuildAgentSnapshot& S = Out.AddDefaulted_GetRef();
		S.Name = Agent->Name;
		S.Address = FString::Printf(TEXT("%s:%d"), *Agent->Host, Agent->Port);
		S.Platform = Agent->Platform;
		S.Engines = Agent->Engines;
		S.Slots = Agent->Slots;
		S.InFlight = Agent->InFlight;
		S.Completed = Agent->Completed;
		S.Failed = Agent->Failed;
		S.bOnline = Agent->bOnline;
		S.bStatic = Agent->bStatic;
		S.bCompatible = Agent->Engines.Contains(EngineKey);
		S.SecondsSinceSeen = Agent->LastSeen > 0.0 ? Now - Agent->LastSeen : -1.0;
	}
}

int32 FDistriBuildAgentPool::CompatibleSlots(const FString& EngineKey) const
{
	FScopeLock Guard(&Lock);
	int32 Total = 0;
	for (const FAgentPtr& Agent : Agents)
	{
		if (Agent->bOnline && Agent->Engines.Contains(EngineKey))
		{
			Total += Agent->Slots;
		}
	}
	return Total;
}
