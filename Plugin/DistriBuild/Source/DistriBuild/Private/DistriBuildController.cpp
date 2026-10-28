#include "DistriBuildController.h"

#include "DistriBuildProtocol.h"
#include "HAL/FileManager.h"
#include "HAL/PlatformProcess.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/QueuedThreadPool.h"
#include "Sockets.h"

namespace
{
	class FDistriBuildJob final : public IQueuedWork
	{
	public:
		FDistriBuildJob(FDistriBuildController& InOwner, const FTaskCommandData& InCommand)
			: Owner(InOwner), Command(InCommand)
		{
		}

		TPromise<FDistributedBuildTaskResult> Promise;

		virtual void DoThreadedWork() override
		{
			Promise.SetValue(Owner.Execute(Command));
			delete this;
		}

		virtual void Abandon() override
		{
			FDistributedBuildTaskResult Result;
			Result.ReturnCode = -1;
			Result.bCompleted = false;
			Promise.SetValue(Result);
			delete this;
		}

	private:
		FDistriBuildController& Owner;
		FTaskCommandData Command;
	};

	FString ResolvePath(const FString& WorkingDirectory, const FString& File)
	{
		return FPaths::IsRelative(File) ? FPaths::Combine(WorkingDirectory, File) : File;
	}
}

FDistriBuildController::FDistriBuildController(FDistriBuildAgentPool& InPool, const FDistriBuildConfig& InConfig)
	: Pool(InPool)
	, Config(InConfig)
{
	TransferDirectory = FPaths::ConvertRelativePathToFull(FPaths::ProjectIntermediateDir() / TEXT("DistriBuild")
		/ FString::Printf(TEXT("%u"), FPlatformProcess::GetCurrentProcessId()));
	IFileManager::Get().MakeDirectory(*TransferDirectory, true);
}

FDistriBuildController::~FDistriBuildController()
{
	Shutdown();
}

void FDistriBuildController::Shutdown()
{
	if (Workers)
	{
		Workers->Destroy(); // abandons queued jobs, their futures complete with bCompleted = false
		delete Workers;
		Workers = nullptr;
	}
	bInitialized = false;
	if (!TransferDirectory.IsEmpty())
	{
		IFileManager::Get().DeleteDirectory(*TransferDirectory, false, true);
	}
}

FDistriBuildConfig FDistriBuildController::GetConfig() const
{
	FScopeLock Guard(&ConfigLock);
	return Config;
}

void FDistriBuildController::ApplyConfig(const FDistriBuildConfig& InConfig)
{
	FScopeLock Guard(&ConfigLock);
	Config = InConfig;
}

FString FDistriBuildController::GetEngineKey() const
{
	return GetConfig().GetEngineKey();
}

DistriBuildCompat::FIsSupportedReturn FDistriBuildController::IsSupported()
{
	// Asked once while the shader compiling manager starts. Only claim support when at least
	// one compatible agent answered; otherwise the engine keeps its normal local workers.
	if (!bSupportChecked)
	{
		bSupportChecked = true;
		const FDistriBuildConfig Cfg = GetConfig();
		if (Cfg.bEnabled)
		{
			Pool.RefreshBlocking();
			const int32 Slots = Pool.CompatibleSlots(Cfg.GetEngineKey());
			bSupported = Slots > 0;
			UE_LOG(LogDistriBuild, Display, TEXT("DistriBuild: %d remote shader slots for engine %s%s"),
				Slots, *Cfg.GetEngineKey(), bSupported ? TEXT("") : TEXT(" - distributed shader compiling stays off"));
		}
	}
	return bSupported;
}

FString FDistriBuildController::CreateUniqueFilePath()
{
	// Engine asks for job input/output paths here; any local folder works because the files are
	// read and written by this process, then shipped to the agent under fixed names.
	return TransferDirectory / FString::Printf(TEXT("%u.job"), ++NextFileId);
}

void FDistriBuildController::InitializeController()
{
	if (bInitialized)
	{
		return;
	}
	const FDistriBuildConfig Cfg = GetConfig();
	Workers = FQueuedThreadPool::Allocate();
	Workers->Create(Cfg.MaxRemoteJobs, 128 * 1024, TPri_BelowNormal, TEXT("DistriBuildWorker"));
	bInitialized = true;
	UE_LOG(LogDistriBuild, Display, TEXT("DistriBuild controller initialized (%d concurrent jobs)"), Cfg.MaxRemoteJobs);
}

TFuture<FDistributedBuildTaskResult> FDistriBuildController::EnqueueTask(const FTaskCommandData& CommandData)
{
	if (!bInitialized)
	{
		InitializeController();
	}
	FDistriBuildJob* Job = new FDistriBuildJob(*this, CommandData);
	TFuture<FDistributedBuildTaskResult> Future = Job->Promise.GetFuture();
	++Queued;
	Workers->AddQueuedWork(Job);
	return Future;
}

FDistributedBuildTaskResult FDistriBuildController::Execute(const FTaskCommandData& Command)
{
	--Queued;
	++Running;
	const FDistriBuildConfig Cfg = GetConfig();
	const FString EngineKey = Cfg.GetEngineKey();
	const FString WorkingDirectory = DistriBuildCompat::GetWorkingDirectory(Command);
	const FString InputPath = ResolvePath(WorkingDirectory, Command.InputFileName);
	const FString OutputPath = ResolvePath(WorkingDirectory, Command.OutputFileName);

	FDistributedBuildTaskResult Result;
	Result.ReturnCode = -1;
	Result.bCompleted = false;

	bool bDone = false;
	bool bTriedRemote = false;
	for (int32 Attempt = 0; Cfg.bEnabled && Attempt < 3 && !bDone; ++Attempt)
	{
		TSharedPtr<FDistriBuildAgent, ESPMode::ThreadSafe> Agent = Pool.Acquire(EngineKey);
		if (!Agent)
		{
			break;
		}
		bTriedRemote = true;
		const double Start = FPlatformTime::Seconds();
		const EDistriBuildOutcome Outcome = RunRemote(*Agent, Command, InputPath, OutputPath, Cfg);
		Pool.Release(Agent, Outcome);

		if (Outcome == EDistriBuildOutcome::Success)
		{
			bDone = true;
			Result.ReturnCode = 0;
			Result.bCompleted = true;
			++RemoteSucceeded;
			RemoteMilliseconds += int64((FPlatformTime::Seconds() - Start) * 1000.0);
		}
		else if (Outcome != EDistriBuildOutcome::Busy)
		{
			++RemoteFailed;
		}
	}

	if (!bDone && (Cfg.bFallbackToLocal || !Cfg.bEnabled))
	{
		// Why this job is not distributed - shown in the monitor and by the DistriBuild.Stats command.
		if (!Cfg.bEnabled)
		{
			++LocalDisabled;
		}
		else if (bTriedRemote)
		{
			++LocalAfterRemoteFailure;
		}
		else
		{
			++LocalNoAgent;
		}
		Result = RunLocal(Command, Cfg);
	}
	--Running;
	return Result;
}

EDistriBuildOutcome FDistriBuildController::RunRemote(const FDistriBuildAgent& Agent, const FTaskCommandData& Command,
	const FString& InputPath, const FString& OutputPath, const FDistriBuildConfig& Cfg)
{
	TArray<DistriBuildProtocol::FBlob> Inputs;
	DistriBuildProtocol::FBlob& Input = Inputs.AddDefaulted_GetRef();
	Input.Name = TEXT("job.in");
	if (!FFileHelper::LoadFileToArray(Input.Data, *InputPath))
	{
		UE_LOG(LogDistriBuild, Warning, TEXT("Cannot read shader job input %s"), *InputPath);
		return EDistriBuildOutcome::JobFailed;
	}

	FSocket* Socket = DistriBuildProtocol::Connect(Agent.Host, Agent.Port, Cfg.ConnectTimeoutSeconds);
	if (!Socket)
	{
		return EDistriBuildOutcome::NetworkError;
	}

	int64 Sent = 0;
	int64 Received = 0;
	EDistriBuildOutcome Outcome = EDistriBuildOutcome::NetworkError;
	TSharedPtr<FJsonObject> Reply;
	TArray<DistriBuildProtocol::FBlob> Outputs;

	// 1) reserve a slot; the agent may be full because other developers use it too
	TSharedRef<FJsonObject> Reserve = MakeShared<FJsonObject>();
	Reserve->SetStringField(TEXT("type"), TEXT("reserve"));
	Reserve->SetStringField(TEXT("token"), Cfg.AuthToken);
	Reserve->SetStringField(TEXT("kind"), TEXT("shader"));
	if (DistriBuildProtocol::SendFrame(Socket, Reserve, {}, &Sent)
		&& DistriBuildProtocol::RecvFrame(Socket, Cfg.ConnectTimeoutSeconds + 5.0f, Reply, Outputs, &Received))
	{
		if (!Reply->GetBoolField(TEXT("ok")))
		{
			const FString Reason = Reply->GetStringField(TEXT("reason"));
			Outcome = Reason == TEXT("busy") ? EDistriBuildOutcome::Busy : EDistriBuildOutcome::JobFailed;
			if (Reason == TEXT("auth"))
			{
				UE_LOG(LogDistriBuild, Warning, TEXT("Agent %s rejected the auth token"), *Agent.Host);
			}
		}
		else
		{
			// 2) ship the job, wait for the result
			TSharedRef<FJsonObject> Run = MakeShared<FJsonObject>();
			Run->SetStringField(TEXT("type"), TEXT("run"));
			Run->SetStringField(TEXT("kind"), TEXT("shader"));
			Run->SetStringField(TEXT("engine"), Cfg.GetEngineKey());
			Run->SetStringField(TEXT("args"), DistriBuildCompat::GetWorkerExtraArgs(Command));
			Run->SetStringField(TEXT("input"), TEXT("job.in"));
			Run->SetStringField(TEXT("output"), TEXT("job.out"));
			Run->SetStringField(TEXT("caption"), FPaths::GetCleanFilename(InputPath));

			if (DistriBuildProtocol::SendFrame(Socket, Run, Inputs, &Sent)
				&& DistriBuildProtocol::RecvFrame(Socket, Cfg.JobTimeoutSeconds + 30.0f, Reply, Outputs, &Received))
			{
				const int32 ExitCode = Reply->HasField(TEXT("exit_code")) ? int32(Reply->GetNumberField(TEXT("exit_code"))) : -1;
				const DistriBuildProtocol::FBlob* Out = Outputs.FindByPredicate([](const DistriBuildProtocol::FBlob& B) { return B.Name == TEXT("job.out"); });
				if (Reply->GetBoolField(TEXT("ok")) && ExitCode == 0 && Out && FFileHelper::SaveArrayToFile(Out->Data, *OutputPath))
				{
					Outcome = EDistriBuildOutcome::Success;
				}
				else
				{
					Outcome = EDistriBuildOutcome::JobFailed;
					FString Reason;
					Reply->TryGetStringField(TEXT("reason"), Reason);
					FString Output;
					Reply->TryGetStringField(TEXT("stdout"), Output);
					UE_LOG(LogDistriBuild, Warning, TEXT("Shader job failed on %s (exit %d) %s - retrying elsewhere/locally\n%s"),
						*Agent.Host, ExitCode, *Reason, *Output.Right(2000));
				}
			}
		}
	}

	DistriBuildProtocol::Close(Socket);
	BytesSent += Sent;
	BytesReceived += Received;
	return Outcome;
}

FDistributedBuildTaskResult FDistriBuildController::RunLocal(const FTaskCommandData& Command, const FDistriBuildConfig& Cfg)
{
	const FString Params = DistriBuildCompat::GetLocalCommandLine(Command, FPlatformProcess::GetCurrentProcessId());

	const int32 MaxLocal = Cfg.GetLocalFallbackJobs();
	for (;;)
	{
		{
			FScopeLock Guard(&LocalLock);
			if (LocalRunning < MaxLocal)
			{
				++LocalRunning;
				break;
			}
		}
		FPlatformProcess::Sleep(0.02f);
	}

	FDistributedBuildTaskResult Result;
	Result.ReturnCode = -1;
	Result.bCompleted = false;

	FProcHandle Proc = FPlatformProcess::CreateProc(*Command.Command, *Params, false, true, true, nullptr, -1, nullptr, nullptr);
	if (Proc.IsValid())
	{
		FPlatformProcess::WaitForProc(Proc);
		int32 ReturnCode = -1;
		FPlatformProcess::GetProcReturnCode(Proc, &ReturnCode);
		FPlatformProcess::CloseProc(Proc);
		Result.ReturnCode = ReturnCode;
		Result.bCompleted = true;
	}
	else
	{
		UE_LOG(LogDistriBuild, Error, TEXT("Failed to launch %s"), *Command.Command);
	}
	++LocalExecuted;

	FScopeLock Guard(&LocalLock);
	--LocalRunning;
	return Result;
}

FDistriBuildStats FDistriBuildController::GetStats() const
{
	FDistriBuildStats Stats;
	Stats.RemoteSucceeded = RemoteSucceeded;
	Stats.RemoteFailed = RemoteFailed;
	Stats.LocalExecuted = LocalExecuted;
	Stats.LocalNoAgent = LocalNoAgent;
	Stats.LocalAfterRemoteFailure = LocalAfterRemoteFailure;
	Stats.LocalDisabled = LocalDisabled;
	Stats.BytesSent = BytesSent;
	Stats.BytesReceived = BytesReceived;
	Stats.Queued = Queued;
	Stats.Running = Running;
	Stats.RemoteSeconds = double(RemoteMilliseconds) / 1000.0;
	return Stats;
}
