#include "SDistriBuildMonitor.h"

#include "ISettingsModule.h"
#include "Modules/ModuleManager.h"
#include "Styling/AppStyle.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/Text/STextBlock.h"
#include "Widgets/Views/SHeaderRow.h"

#define LOCTEXT_NAMESPACE "DistriBuildMonitor"

namespace DistriBuildColumns
{
	static const FName Name("Name");
	static const FName Address("Address");
	static const FName Platform("Platform");
	static const FName Load("Load");
	static const FName Done("Done");
	static const FName Failed("Failed");
	static const FName Engines("Engines");
	static const FName Status("Status");
}

class SDistriBuildAgentRow : public SMultiColumnTableRow<TSharedPtr<FDistriBuildAgentSnapshot>>
{
public:
	SLATE_BEGIN_ARGS(SDistriBuildAgentRow) {}
	SLATE_END_ARGS()

	void Construct(const FArguments& InArgs, const TSharedRef<STableViewBase>& Owner, TSharedPtr<FDistriBuildAgentSnapshot> InItem)
	{
		Item = InItem;
		SMultiColumnTableRow::Construct(FSuperRowType::FArguments(), Owner);
	}

	virtual TSharedRef<SWidget> GenerateWidgetForColumn(const FName& Column) override
	{
		using namespace DistriBuildColumns;
		const FDistriBuildAgentSnapshot& A = *Item;
		FText Text;
		FSlateColor Color = FSlateColor::UseForeground();

		if (Column == Name)
		{
			Text = FText::FromString(A.Name);
		}
		else if (Column == Address)
		{
			Text = FText::FromString(A.Address + (A.bStatic ? TEXT("") : TEXT("  (discovered)")));
		}
		else if (Column == Platform)
		{
			Text = FText::FromString(A.Platform);
		}
		else if (Column == Load)
		{
			Text = FText::Format(LOCTEXT("LoadFmt", "{0} / {1}"), A.InFlight, A.Slots);
		}
		else if (Column == Done)
		{
			Text = FText::AsNumber(A.Completed);
		}
		else if (Column == Failed)
		{
			Text = FText::AsNumber(A.Failed);
			if (A.Failed > 0)
			{
				Color = FLinearColor(1.0f, 0.55f, 0.2f);
			}
		}
		else if (Column == Engines)
		{
			Text = FText::FromString(FString::Join(A.Engines, TEXT(", ")));
		}
		else if (Column == Status)
		{
			if (!A.bOnline)
			{
				Text = LOCTEXT("Offline", "Offline");
				Color = FLinearColor(0.9f, 0.25f, 0.25f);
			}
			else if (!A.bCompatible)
			{
				Text = LOCTEXT("Incompatible", "No matching engine");
				Color = FLinearColor(0.95f, 0.75f, 0.2f);
			}
			else
			{
				Text = A.InFlight > 0 ? LOCTEXT("Working", "Working") : LOCTEXT("Idle", "Idle");
				Color = FLinearColor(0.3f, 0.85f, 0.4f);
			}
		}

		return SNew(SBox).Padding(FMargin(6.0f, 2.0f))
			[
				SNew(STextBlock).Text(Text).ColorAndOpacity(Color)
			];
	}

private:
	TSharedPtr<FDistriBuildAgentSnapshot> Item;
};

void SDistriBuildMonitor::Construct(const FArguments& InArgs)
{
	using namespace DistriBuildColumns;

	ChildSlot
	[
		SNew(SVerticalBox)
		+ SVerticalBox::Slot().AutoHeight().Padding(8.0f)
		[
			SNew(SHorizontalBox)
			+ SHorizontalBox::Slot().FillWidth(1.0f).VAlign(VAlign_Center)
			[
				SNew(SVerticalBox)
				+ SVerticalBox::Slot().AutoHeight()
				[
					SNew(STextBlock)
					.Font(FAppStyle::GetFontStyle("HeadingExtraSmall"))
					.Text(this, &SDistriBuildMonitor::GetSummaryText)
				]
				+ SVerticalBox::Slot().AutoHeight().Padding(0.0f, 4.0f, 0.0f, 0.0f)
				[
					SNew(STextBlock).Text(this, &SDistriBuildMonitor::GetStatsText)
				]
			]
			+ SHorizontalBox::Slot().AutoWidth().Padding(4.0f, 0.0f)
			[
				SNew(SButton)
				.Text(LOCTEXT("Rescan", "Rescan Agents"))
				.OnClicked_Lambda([]()
				{
					if (IDistriBuildModule* Module = IDistriBuildModule::GetPtr())
					{
						Module->RefreshAgents();
					}
					return FReply::Handled();
				})
			]
			+ SHorizontalBox::Slot().AutoWidth().Padding(4.0f, 0.0f)
			[
				SNew(SButton)
				.Text(LOCTEXT("Settings", "Settings"))
				.OnClicked_Lambda([]()
				{
					FModuleManager::LoadModuleChecked<ISettingsModule>("Settings").ShowViewer("Project", "Plugins", "DistriBuild");
					return FReply::Handled();
				})
			]
		]
		+ SVerticalBox::Slot().FillHeight(1.0f).Padding(8.0f, 0.0f, 8.0f, 8.0f)
		[
			SNew(SBorder)
			.BorderImage(FAppStyle::GetBrush("ToolPanel.GroupBorder"))
			[
				SAssignNew(List, SListView<FItem>)
				.ListItemsSource(&Items)
				.OnGenerateRow(this, &SDistriBuildMonitor::MakeRow)
				.SelectionMode(ESelectionMode::None)
				.HeaderRow
				(
					SNew(SHeaderRow)
					+ SHeaderRow::Column(Name).DefaultLabel(LOCTEXT("ColName", "Agent")).FillWidth(1.2f)
					+ SHeaderRow::Column(Address).DefaultLabel(LOCTEXT("ColAddress", "Address")).FillWidth(1.6f)
					+ SHeaderRow::Column(Platform).DefaultLabel(LOCTEXT("ColPlatform", "OS")).FillWidth(0.6f)
					+ SHeaderRow::Column(Load).DefaultLabel(LOCTEXT("ColLoad", "Busy / Slots")).FillWidth(0.8f)
					+ SHeaderRow::Column(Done).DefaultLabel(LOCTEXT("ColDone", "Done")).FillWidth(0.6f)
					+ SHeaderRow::Column(Failed).DefaultLabel(LOCTEXT("ColFailed", "Failed")).FillWidth(0.6f)
					+ SHeaderRow::Column(Engines).DefaultLabel(LOCTEXT("ColEngines", "Engines")).FillWidth(1.2f)
					+ SHeaderRow::Column(Status).DefaultLabel(LOCTEXT("ColStatus", "Status")).FillWidth(1.0f)
				)
			]
		]
	];

	Poll(0.0, 0.0f);
	RegisterActiveTimer(1.0f, FWidgetActiveTimerDelegate::CreateSP(this, &SDistriBuildMonitor::Poll));
}

EActiveTimerReturnType SDistriBuildMonitor::Poll(double InCurrentTime, float InDeltaTime)
{
	IDistriBuildModule* Module = IDistriBuildModule::GetPtr();
	if (!Module)
	{
		return EActiveTimerReturnType::Continue;
	}

	TArray<FDistriBuildAgentSnapshot> Agents;
	Module->GetAgents(Agents);
	Agents.Sort([](const FDistriBuildAgentSnapshot& A, const FDistriBuildAgentSnapshot& B) { return A.Name < B.Name; });

	Items.Reset();
	TotalSlots = 0;
	UsableSlots = 0;
	for (FDistriBuildAgentSnapshot& Agent : Agents)
	{
		TotalSlots += Agent.bOnline ? Agent.Slots : 0;
		UsableSlots += (Agent.bOnline && Agent.bCompatible) ? Agent.Slots : 0;
		Items.Add(MakeShared<FDistriBuildAgentSnapshot>(MoveTemp(Agent)));
	}
	Stats = Module->GetStats();
	if (List.IsValid())
	{
		List->RequestListRefresh();
	}
	return EActiveTimerReturnType::Continue;
}

TSharedRef<ITableRow> SDistriBuildMonitor::MakeRow(FItem Item, const TSharedRef<STableViewBase>& Owner)
{
	return SNew(SDistriBuildAgentRow, Owner, Item);
}

FText SDistriBuildMonitor::GetSummaryText() const
{
	IDistriBuildModule* Module = IDistriBuildModule::GetPtr();
	const bool bActive = Module && Module->IsControllerActive();
	return FText::Format(LOCTEXT("Summary", "Shader distribution: {0}    Agents: {1}    Usable slots: {2} / {3}    Engine: {4}"),
		bActive ? LOCTEXT("On", "ON") : LOCTEXT("Off", "OFF (no compatible agent at startup, or disabled)"),
		Items.Num(), UsableSlots, TotalSlots, FText::FromString(Module ? Module->GetEngineKey() : FString()));
}

FText SDistriBuildMonitor::GetStatsText() const
{
	const double TotalJobs = double(Stats.RemoteSucceeded + Stats.LocalExecuted);
	const double RemoteShare = TotalJobs > 0.0 ? 100.0 * double(Stats.RemoteSucceeded) / TotalJobs : 0.0;
	return FText::Format(LOCTEXT("Stats", "Queued {0}  Running {1}  |  Remote OK {2} ({3}%)  Remote failed {4}  Local {5}  |  Sent {6}  Received {7}"),
		Stats.Queued, Stats.Running, Stats.RemoteSucceeded, FText::AsNumber(FMath::RoundToInt(RemoteShare)),
		Stats.RemoteFailed, Stats.LocalExecuted, FText::AsMemory(Stats.BytesSent), FText::AsMemory(Stats.BytesReceived));
}

#undef LOCTEXT_NAMESPACE
