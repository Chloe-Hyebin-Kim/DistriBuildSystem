#pragma once

// Engine-version shims for IDistributedBuildController / FTaskCommandData.
//
// The interface has drifted between engine versions (4.26 ... 5.x): return types of IsSupported()
// and GetName() changed between `bool`/`const bool` and `FString`/`const FString`, and the task
// struct has carried either a full `CommandArgs` string or separate `WorkingDirectory`,
// `ExtraCommandArgs` and `DispatcherPID` fields. Everything here is resolved at compile time from
// the engine's own header, so one source tree builds against all of them. Written in C++14 so it
// also compiles with 4.26's default language standard.

#include "CoreMinimal.h"
#include "DistributedBuildControllerInterface.h"
#include "Misc/Paths.h"
#include <type_traits>
#include <utility>

namespace DistriBuildCompat
{
	template <typename T> struct TMemberReturn;
	template <typename C, typename R> struct TMemberReturn<R (C::*)()> { using Type = R; };
	template <typename C, typename R> struct TMemberReturn<R (C::*)() const> { using Type = R; };

	/** Exact return types (including a top-level const) as declared by this engine's interface. */
	using FIsSupportedReturn = typename TMemberReturn<decltype(&IDistributedBuildController::IsSupported)>::Type;
	using FGetNameReturn = typename TMemberReturn<decltype(&IDistributedBuildController::GetName)>::Type;

	template <typename...> struct TVoid { using Type = void; };

#define DISTRIBUILD_DETECT_MEMBER(Member) \
	template <typename T, typename = void> struct THas##Member : std::false_type {}; \
	template <typename T> struct THas##Member<T, typename TVoid<decltype(std::declval<T&>().Member)>::Type> : std::true_type {};

	DISTRIBUILD_DETECT_MEMBER(CommandArgs)
	DISTRIBUILD_DETECT_MEMBER(ExtraCommandArgs)
	DISTRIBUILD_DETECT_MEMBER(WorkingDirectory)
	DISTRIBUILD_DETECT_MEMBER(DispatcherPID)

#undef DISTRIBUILD_DETECT_MEMBER

	namespace Private
	{
		template <typename T> FString CommandArgs(const T& D, std::true_type) { return D.CommandArgs; }
		template <typename T> FString CommandArgs(const T&, std::false_type) { return FString(); }

		template <typename T> FString ExtraArgs(const T& D, std::true_type) { return D.ExtraCommandArgs; }
		template <typename T> FString ExtraArgs(const T&, std::false_type) { return FString(); }

		template <typename T> FString WorkingDir(const T& D, std::true_type) { return D.WorkingDirectory; }
		template <typename T> FString WorkingDir(const T& D, std::false_type) { return FPaths::GetPath(D.InputFileName); }

		template <typename T> uint32 DispatcherPid(const T& D, std::true_type) { return uint32(D.DispatcherPID); }
		template <typename T> uint32 DispatcherPid(const T&, std::false_type) { return 0; }

		/** Text after the first Count whitespace-separated (quote-aware) tokens. */
		inline FString SkipTokens(const FString& Text, int32 Count)
		{
			int32 Index = 0;
			const int32 Length = Text.Len();
			for (int32 Token = 0; Token < Count; ++Token)
			{
				while (Index < Length && FChar::IsWhitespace(Text[Index]))
				{
					++Index;
				}
				bool bQuoted = false;
				while (Index < Length && (bQuoted || !FChar::IsWhitespace(Text[Index])))
				{
					if (Text[Index] == TEXT('"'))
					{
						bQuoted = !bQuoted;
					}
					++Index;
				}
			}
			return Text.Mid(Index).TrimStartAndEnd();
		}
	}

	inline FString GetWorkingDirectory(const FTaskCommandData& D)
	{
		return Private::WorkingDir(D, THasWorkingDirectory<FTaskCommandData>());
	}

	inline uint32 GetDispatcherPid(const FTaskCommandData& D)
	{
		return Private::DispatcherPid(D, THasDispatcherPID<FTaskCommandData>());
	}

	/**
	 * ShaderCompileWorker arguments after the five positional ones
	 * (WorkingDir/ ParentPID ThreadId Input Output), e.g. "-xge_int -subprocess-args...".
	 */
	inline FString GetWorkerExtraArgs(const FTaskCommandData& D)
	{
		const FString Extra = Private::ExtraArgs(D, THasExtraCommandArgs<FTaskCommandData>());
		if (!Extra.IsEmpty())
		{
			return Extra;
		}
		const FString Full = Private::CommandArgs(D, THasCommandArgs<FTaskCommandData>());
		return Full.IsEmpty() ? FString() : Private::SkipTokens(Full, 5);
	}

	/** Full ShaderCompileWorker command line for running the task on this machine. */
	inline FString GetLocalCommandLine(const FTaskCommandData& D, uint32 FallbackPid)
	{
		const FString Full = Private::CommandArgs(D, THasCommandArgs<FTaskCommandData>());
		if (!Full.IsEmpty())
		{
			return Full;
		}
		FString WorkingDirectory = GetWorkingDirectory(D);
		while (WorkingDirectory.EndsWith(TEXT("/")) || WorkingDirectory.EndsWith(TEXT("\\")))
		{
			WorkingDirectory = WorkingDirectory.LeftChop(1);
		}
		const uint32 Pid = GetDispatcherPid(D) != 0 ? GetDispatcherPid(D) : FallbackPid;
		return FString::Printf(TEXT("\"%s/\" %u 0 \"%s\" \"%s\" %s"), *WorkingDirectory, Pid,
			*FPaths::GetCleanFilename(D.InputFileName), *FPaths::GetCleanFilename(D.OutputFileName),
			*Private::ExtraArgs(D, THasExtraCommandArgs<FTaskCommandData>()));
	}
}
